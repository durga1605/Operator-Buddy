"""
One active API poller per machine.

Machine KM-TU-16 → at most one RUNNING poll thread.
Different machines may poll in parallel.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any

from asgiref.sync import async_to_sync
from bson import ObjectId
from django.conf import settings

from Core.components.db_connection_string import get_db_connection
from Core.functions.mtlink_api import fetch_machine_counter, machine_group_name

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("RUNNING", "IN_PROGRESS")
POLL_INTERVAL_SEC = float(getattr(settings, "MTLINK_POLL_INTERVAL_SEC", 2.0))

_lock = threading.Lock()
# machine_id -> {"stop": Event, "thread": Thread, "session_id": str, "plant_code": str}
_pollers: dict[str, dict[str, Any]] = {}


def is_polling(machine_id: str) -> bool:
    mid = machine_id.strip()
    with _lock:
        entry = _pollers.get(mid)
        return bool(entry and entry["thread"].is_alive())


def stop_machine_poller(machine_id: str) -> None:
    mid = machine_id.strip()
    with _lock:
        entry = _pollers.pop(mid, None)
    if not entry:
        return
    entry["stop"].set()
    thread = entry.get("thread")
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=5)


def _broadcast(machine_id: str, payload: dict) -> None:
    """Push update to WebSocket group machine_{id}. No-op if Channels missing."""
    try:
        from channels.layers import get_channel_layer
    except ImportError:
        return

    channel_layer = get_channel_layer()
    if channel_layer is None:
        return

    group = machine_group_name(machine_id)
    try:
        async_to_sync(channel_layer.group_send)(
            group,
            {"type": "machine.production_update", "payload": payload},
        )
    except Exception:
        logger.exception("WS broadcast failed for %s", group)


def _complete_session(
    db, session: dict, production_count: int, machine_counter: int
) -> dict:
    """Mark session COMPLETED (qty reached), keep history, release machine."""
    now = datetime.now()
    update = {
        "production_count": production_count,
        "machine_counter": machine_counter,
        "status": "COMPLETED",
        "qty_reached": True,
        "sap_posted": False,
        "poll_active": False,
        "end_time": now,
        "timestamp": now,
    }
    start_time = session.get("start_time") or session.get("timestamp")
    if start_time:
        update["duration_seconds"] = max(0, int((now - start_time).total_seconds()))

    db["PCB_Trace"].update_one({"_id": session["_id"]}, {"$set": update})
    return {**session, **update}


def _poll_loop(
    machine_id: str, plant_code: str, session_id: str, stop_event: threading.Event
) -> None:
    mid = machine_id.strip()
    logger.info("Poll start machine=%s session=%s", mid, session_id)

    while not stop_event.wait(POLL_INTERVAL_SEC):
        try:
            db = get_db_connection(plant_code)
            session = db["PCB_Trace"].find_one({"_id": ObjectId(session_id)})
            if not session:
                logger.warning("Session gone — stop poll machine=%s", mid)
                break

            status = session.get("status")
            if status not in ACTIVE_STATUSES or session.get("poll_active") is False:
                logger.info(
                    "Session not active — stop poll machine=%s status=%s", mid, status
                )
                break

            baseline = int(session.get("baseline_count", 0) or 0)
            wo_qty = int(session.get("woqty", 0) or 0)
            if wo_qty <= 0:
                logger.error("Invalid woqty on session %s — stop poll", session_id)
                break

            try:
                current = fetch_machine_counter(mid)
            except RuntimeError as exc:
                logger.warning("Counter read fail machine=%s: %s", mid, exc)
                _broadcast(
                    mid,
                    {
                        "event": "poll_error",
                        "machine_id": mid,
                        "work_order": session.get("work_order"),
                        "error": str(exc),
                        "production_count": int(
                            session.get("production_count", 0) or 0
                        ),
                        "woqty": wo_qty,
                        "status": status,
                    },
                )
                continue

            production_count = max(0, current - baseline)
            production_count = min(production_count, wo_qty)

            db["PCB_Trace"].update_one(
                {"_id": session["_id"]},
                {
                    "$set": {
                        "production_count": production_count,
                        "machine_counter": current,
                        "last_poll_at": datetime.now(),
                    }
                },
            )

            payload = {
                "event": "production_update",
                "machine_id": mid,
                "work_order": session.get("work_order"),
                "session_id": session_id,
                "baseline_count": baseline,
                "machine_counter": current,
                "production_count": production_count,
                "woqty": wo_qty,
                "status": status,
            }

            if production_count >= wo_qty:
                completed = _complete_session(db, session, wo_qty, current)
                end_time = completed.get("end_time")
                payload.update(
                    {
                        "event": "completed",
                        "production_count": wo_qty,
                        "status": "COMPLETED",
                        "qty_reached": True,
                        "end_time": end_time.isoformat() if end_time else None,
                    }
                )
                _broadcast(mid, payload)
                logger.info(
                    "WO qty reached machine=%s wo=%s qty=%s — stop poll",
                    mid,
                    session.get("work_order"),
                    wo_qty,
                )
                break

            _broadcast(mid, payload)
        except Exception:
            logger.exception("Poll loop error machine=%s", mid)

    with _lock:
        entry = _pollers.get(mid)
        if entry and entry.get("session_id") == session_id:
            _pollers.pop(mid, None)
    logger.info("Poll stop machine=%s session=%s", mid, session_id)


def start_machine_poller(machine_id: str, plant_code: str, session_id: str) -> bool:
    """
    Start poll for machine if none running.
    Returns True if started, False if already polling.
    """
    mid = machine_id.strip()
    with _lock:
        existing = _pollers.get(mid)
        if existing and existing["thread"].is_alive():
            logger.info(
                "Poll already active machine=%s session=%s — skip",
                mid,
                existing.get("session_id"),
            )
            return False

        stop_event = threading.Event()
        thread = threading.Thread(
            target=_poll_loop,
            args=(mid, plant_code, session_id, stop_event),
            name=f"mtlink-poll-{mid}",
            daemon=True,
        )
        _pollers[mid] = {
            "stop": stop_event,
            "thread": thread,
            "session_id": session_id,
            "plant_code": plant_code,
        }
        thread.start()
        return True
