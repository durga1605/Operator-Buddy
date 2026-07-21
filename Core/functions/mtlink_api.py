"""MTLINK machine counter API helpers."""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)


def machine_group_name(machine_id: str) -> str:
    """WebSocket channel group for one machine."""
    return f"machine_{machine_id.strip()}"


def _base_url() -> str:
    return (getattr(settings, "MTLINK_API_BASE_URL", "") or "").rstrip("/")


def build_parts_num_url(machine: str, *, from_iso: str | None = None) -> str:
    url = f"{_base_url()}/{machine}/monitorings/PartsNum_path1_{machine}/logs"
    if from_iso:
        return f"{url}?{urllib.parse.urlencode({'from': from_iso})}"
    return url


def build_part_counter_url(machine: str, *, from_iso: str | None = None) -> str:
    url = f"{_base_url()}/{machine}/monitorings/PartCounter_{machine}/logs"
    if from_iso:
        return f"{url}?{urllib.parse.urlencode({'from': from_iso})}"
    return url


def _lookback_from_iso() -> str:
    hours = float(getattr(settings, "MTLINK_LOG_LOOKBACK_HOURS", 6) or 6)
    # API `from` filter expects UTC ISO timestamps
    dt = datetime.now(timezone.utc) - timedelta(hours=max(0.1, hours))
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _auth_headers() -> dict[str, str]:
    """Build Authorization headers. MTLINK uses HTTP Basic Auth."""
    headers: dict[str, str] = {}
    extra = getattr(settings, "MTLINK_API_HEADERS", None) or {}
    headers.update(extra)

    user = (getattr(settings, "MTLINK_API_USER", None) or "").strip()
    password = getattr(settings, "MTLINK_API_PASSWORD", None) or ""
    if user:
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
        return headers

    bearer = (getattr(settings, "MTLINK_API_TOKEN", None) or "").strip()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    return headers


def _extract_counter(payload: Any) -> int | None:
    """
    Parse MTLINK log JSON → latest non-null counter.

    Log shape:
      [{"start": "...", "end": "...", "value": 176}, ...]
    """
    if payload is None:
        return None
    if isinstance(payload, bool):
        return None
    if isinstance(payload, (int, float)):
        return int(payload)
    if isinstance(payload, str):
        try:
            return int(float(payload.strip()))
        except ValueError:
            return None
    if isinstance(payload, dict):
        if "value" in payload:
            val = payload.get("value")
            if val is None:
                return None
            return _extract_counter(val)
        for key in (
            "Value",
            "count",
            "Count",
            "parts",
            "PartsNum",
            "PartCounter",
            "data",
            "result",
            "logs",
            "items",
            "records",
            "Rows",
        ):
            if key in payload:
                found = _extract_counter(payload[key])
                if found is not None:
                    return found
        return None
    if isinstance(payload, list):
        if not payload:
            return None
        # Newest last — walk backward, skip null values
        for item in reversed(payload):
            found = _extract_counter(item)
            if found is not None:
                return found
    return None


def _http_get_json(url: str, timeout: float | None = None) -> Any:
    if timeout is None:
        timeout = float(getattr(settings, "MTLINK_HTTP_TIMEOUT_SEC", 30) or 30)
    req = urllib.request.Request(url, method="GET")
    for name, value in _auth_headers().items():
        req.add_header(name, value)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    if not raw.strip():
        return None
    return json.loads(raw)


def fetch_machine_counter(machine_id: str) -> int:
    """
    Read current machine part counter.

    Prefer PartsNum with `?from=` lookback (full history is multi-MB and slow).
    Fall back to PartCounter, then unfiltered PartsNum.
    """
    machine = machine_id.strip()
    if not machine:
        raise RuntimeError("Machine ID required")

    if not _base_url():
        raise RuntimeError("MTLINK_API_BASE_URL not configured")

    user = (getattr(settings, "MTLINK_API_USER", None) or "").strip()
    if not user and not (getattr(settings, "MTLINK_API_TOKEN", None) or "").strip():
        raise RuntimeError(
            "MTLINK auth missing. Set MTLINK_API_USER and MTLINK_API_PASSWORD in Config/.env"
        )

    from_iso = _lookback_from_iso()
    # PartCounter often empty; PartsNum full dump ~6MB — try filtered first.
    candidates = (
        build_parts_num_url(machine, from_iso=from_iso),
        build_part_counter_url(machine, from_iso=from_iso),
        build_parts_num_url(machine),
        build_part_counter_url(machine),
    )

    errors: list[str] = []
    for url in candidates:
        try:
            payload = _http_get_json(url)
            counter = _extract_counter(payload)
            if counter is not None:
                logger.info(
                    "MTLINK counter machine=%s value=%s url=%s", machine, counter, url
                )
                return counter
            errors.append(f"{url}: no counter in response")
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            json.JSONDecodeError,
            ValueError,
            OSError,
        ) as exc:
            errors.append(f"{url}: {exc}")
            logger.warning("MTLINK fetch failed for %s: %s", url, exc)

    raise RuntimeError("Unable to read machine counter. " + "; ".join(errors[-3:]))
