"""WIP start/complete process API views (work-order-first flow)."""

import json
import logging
from datetime import datetime

from bson import ObjectId
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET

from Core.auth.logs import traceability_logs
from Core.components.db_connection_string import get_db_connection
from Core.functions.mtlink_api import fetch_machine_counter, machine_group_name
from Core.functions.sap_bapi_fetch import fetch_process_details, post_to_sap_prodent_ot
from Core.functions.wip_helpers import (
    find_part_document,
    get_sap_plant_code,
    has_pcb_trace_completion,
    match_sap_line,
    parse_sap_work_order_lines,
    summarize_sap_work_order,
)
from Core.services.machine_poller import (
    ACTIVE_STATUSES,
    is_polling,
    start_machine_poller,
    stop_machine_poller,
)

logger = logging.getLogger(__name__)

PLANT_CODES = ["002", "034", "143", "123", "041"]


def _find_running_on_machine(db, machine_id: str):
    """Return active RUNNING/IN_PROGRESS session for machine, if any."""
    return db["PCB_Trace"].find_one(
        {"machine_id": machine_id.strip(), "status": {"$in": list(ACTIVE_STATUSES)}}
    )


def _session_public(doc: dict) -> dict:
    """Serialize session fields for API / WS clients."""
    if not doc:
        return {}
    start_time = doc.get("start_time") or doc.get("timestamp")
    elapsed = 0
    if start_time:
        elapsed = max(0, int((datetime.now() - start_time).total_seconds()))
    return {
        "session_id": str(doc.get("_id", "")),
        "work_order": doc.get("work_order", ""),
        "operator_id": doc.get("operator_id", ""),
        "machine_id": doc.get("machine_id", ""),
        "process_name": doc.get("process_name", ""),
        "material_code": doc.get("material_code", ""),
        "part_no": doc.get("part_no", ""),
        "woqty": doc.get("woqty"),
        "production_count": int(doc.get("production_count", 0) or 0),
        "rejected_count": int(doc.get("rejected_count", 0) or 0),
        "baseline_count": doc.get("baseline_count"),
        "machine_counter": doc.get("machine_counter"),
        "status": doc.get("status"),
        "qty_reached": bool(doc.get("qty_reached")),
        "sap_posted": bool(doc.get("sap_posted")),
        "poll_active": bool(doc.get("poll_active", False)),
        "ws_group": machine_group_name(doc.get("machine_id", "")),
        "elapsed_seconds": elapsed,
    }


def _parse_json_body(request):
    """Return JSON body dict or None if invalid."""
    try:
        return json.loads(request.body)
    except json.JSONDecodeError:
        return None


def _plant_required(request):
    """Return plant_code or error response tuple (code, response)."""
    plant_code = request.session.get("plant_code")
    if not plant_code:
        return None, JsonResponse({"error": "Plant code not in session"}, status=400)
    return plant_code, None


def _fetch_sap_lines(work_order, sap_plant_code):
    """Call SAP and return normalized lines or error response."""
    result, bapi_ok, error_msg, woqty, material_plant_list = fetch_process_details(
        work_order,
        sap_plant_code,
    )
    if not bapi_ok or not result:
        return None, JsonResponse(
            {"error": error_msg or "Work order not found in SAP."},
            status=400,
        )

    lines = parse_sap_work_order_lines(result)
    return (result, lines, woqty, material_plant_list), None


def normalize_operator_id(op):
    """Normalize operator id from scanner input."""
    return str(op or "").strip()


def determine_shift(current_dt):
    """Return shift label for a timestamp."""
    from datetime import time

    current_time = current_dt.time()
    shift_1_start, shift_1_end = time(8, 0), time(16, 30)
    shift_2_start, shift_2_end = time(16, 30), time(1, 0)
    shift_3_start, shift_3_end = time(1, 0), time(8, 0)
    if shift_1_start <= current_time < shift_1_end:
        return "Shift 1"
    if current_time >= shift_2_start or current_time < shift_2_end:
        return "Shift 2"
    if shift_3_start <= current_time < shift_3_end:
        return "Shift 3"
    return "Unknown"


def plant_select(request):
    """Manual plant selection (legacy flow)."""
    if request.method == "POST":
        plant_code = request.POST.get("plant_code", "").strip()
        if plant_code in PLANT_CODES:
            request.session["plant_code"] = plant_code
            return redirect("wip_scan_page")
        return render(
            request,
            "wip/plant_select.html",
            {"error": "Invalid plant code", "plants": PLANT_CODES},
        )
    return render(request, "wip/plant_select.html", {"plants": PLANT_CODES})


def wip_scan_page(request):
    """Render WIP scan screen; requires plant in session."""
    plant_code = request.session.get("plant_code")
    if not plant_code:
        return render(
            request,
            "wip/wip.html",
            {"error": "Plant code required. Select a plant first."},
        )
    return render(request, "wip/wip.html")


def scan_work_order(request):
    """
    Scan work order first.

    Priority order:
    1. IN_PROGRESS record exists  → resume timer, jump to counts
    2. COMPLETED record exists with remaining balance (per process)
                                  → pre-fill all fields, jump to counts
    3. Normal new scan            → SAP fetch, walk through operator/machine/process
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    plant_code, err = _plant_required(request)
    if err:
        return err

    data = _parse_json_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    work_order = data.get("work_order", "").strip()
    if not work_order:
        return JsonResponse({"error": "Work Order required"}, status=400)

    db = get_db_connection(plant_code)

    # ── Priority 1: active RUNNING / IN_PROGRESS ────────────────────
    in_progress = db["PCB_Trace"].find_one(
        {"work_order": work_order, "status": {"$in": list(ACTIVE_STATUSES)}}
    )
    if in_progress:
        # Ensure poller alive for this machine (e.g. after server restart)
        machine_id = in_progress.get("machine_id", "")
        session_id = str(in_progress["_id"])
        if machine_id and not is_polling(machine_id):
            start_machine_poller(machine_id, plant_code, session_id)
        return JsonResponse(
            {
                "status": "in_progress",
                "in_progress_data": _session_public(in_progress),
            }
        )

    # Qty reached, awaiting SAP submit (machine already released)
    awaiting_sap = db["PCB_Trace"].find_one(
        {
            "work_order": work_order,
            "status": "COMPLETED",
            "qty_reached": True,
            "sap_posted": {"$ne": True},
        }
    )
    if awaiting_sap:
        data = _session_public(awaiting_sap)
        data["awaiting_sap"] = True
        return JsonResponse({"status": "in_progress", "in_progress_data": data})

    # ── Priority 2: check for partial COMPLETED (balance remaining) ──
    # Find the most recent COMPLETED record for this WO.
    # We group by (machine_id, process_name) and check if any process
    # still has remaining balance.
    completed_records = list(
        db["PCB_Trace"]
        .find(
            {
                "work_order": work_order,
                "status": "COMPLETED",
                "$or": [
                    {"sap_posted": True},
                    {"sap_response": {"$exists": True}},
                    {"qty_reached": {"$ne": True}},
                ],
            }
        )
        .sort("timestamp", -1)
    )

    if completed_records:
        # Build per-process totals
        from collections import defaultdict

        process_totals = defaultdict(int)
        process_last_record = {}
        for r in completed_records:
            key = (r.get("machine_id", ""), r.get("process_name", ""))
            process_totals[key] += int(r.get("production_count", 0) or 0) + int(
                r.get("rejected_count", 0) or 0
            )
            if key not in process_last_record:
                process_last_record[key] = r  # most recent (sorted desc)

        # Find a process key that still has balance
        partial_key = None
        partial_record = None
        partial_balance = 0
        for key, last_rec in process_last_record.items():
            woqty = int(last_rec.get("woqty") or 0)
            if woqty > 0 and process_totals[key] < woqty:
                partial_key = key
                partial_record = last_rec
                partial_balance = woqty - process_totals[key]
                break

        if partial_record:
            # Return partial status — JS will skip all scanning steps
            woqty = int(partial_record.get("woqty") or 0)
            completed_qty = process_totals[partial_key]
            return JsonResponse(
                {
                    "status": "partial",
                    "partial_data": {
                        "record_id": str(partial_record["_id"]),
                        "work_order": work_order,
                        "operator_id": partial_record.get("operator_id", ""),
                        "machine_id": partial_record.get("machine_id", ""),
                        "process_name": partial_record.get("process_name", ""),
                        "material_code": partial_record.get("material_code", ""),
                        "part_no": partial_record.get("part_no", ""),
                        "woqty": woqty,
                        "completed_qty": completed_qty,
                        "balance_qty": partial_balance,
                    },
                }
            )

    # ── Priority 3: fresh scan — hit SAP ────────────────────────────
    sap_plant_code = get_sap_plant_code(db, plant_code)
    sap_pack, err = _fetch_sap_lines(work_order, sap_plant_code)
    if err:
        return err

    _result, lines, woqty, _ = sap_pack
    material_code, part_no, summary_qty, process_names, _ = summarize_sap_work_order(
        _result
    )
    display_qty = summary_qty or woqty

    request.session["wip_work_order"] = work_order
    request.session["wip_sap_plant_code"] = sap_plant_code
    request.session["wip_material_code"] = material_code
    request.session["wip_part_no"] = part_no

    part_doc = find_part_document(db, part_no=part_no, material_code=material_code)
    part_name = part_doc.get("part_name", part_no) if part_doc else part_no

    return JsonResponse(
        {
            "status": "success",
            "work_order": work_order,
            "woqty": display_qty,
            "material_code": material_code,
            "part_no": part_no,
            "part_name": part_name,
            "sap_plant_code": sap_plant_code,
            "processes": process_names,
            "sap_lines": [
                {
                    "process_description": line["process_description"],
                    "work_center": line["work_center"],
                    "stock_qty": line["stock_qty"],
                    "completed_in_sap": line["completed_in_sap"],
                    "ip_description": line.get("ip_description", ""),
                    "op_description": line.get("op_description", ""),
                    "alt_bom": line.get("alt_bom", ""),
                }
                for line in lines
            ],
        }
    )


def scan_operator(request):
    """Validate scanned operator id."""
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    data = _parse_json_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    operator_id = data.get("operator_id", "").strip()
    if not operator_id:
        return JsonResponse({"error": "Operator ID required"}, status=400)
    return JsonResponse({"status": "success", "operator_id": operator_id})


def validate_machine_process(request):
    """
    Validate machine + process against PMS_oee_cell and check PCB_Trace duplicates.
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    plant_code, err = _plant_required(request)
    if err:
        return err

    data = _parse_json_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    work_order = data.get("work_order", "").strip() or request.session.get(
        "wip_work_order", ""
    )
    machine_id = data.get("machine_id", "").strip()
    alt_bom = data.get("alt_bom", "").strip()
    process_name = (
        data.get("process_name", "").strip() or data.get("process_selected", "").strip()
    )

    if not work_order:
        return JsonResponse({"error": "Work Order required"}, status=400)
    if not process_name:
        return JsonResponse({"error": "Process selection required"}, status=400)

    db = get_db_connection(plant_code)
    sap_plant_code = request.session.get("wip_sap_plant_code") or get_sap_plant_code(
        db, plant_code
    )

    sap_pack, err = _fetch_sap_lines(work_order, sap_plant_code)
    if err:
        return err

    _, lines, _, _ = sap_pack

    # Retrieve material and part info from session
    material_code = request.session.get("wip_material_code")
    part_no = request.session.get("wip_part_no")

    # Check if there is an active process in progress in the database
    in_progress_process = db["PCB_Trace"].find_one(
        {"work_order": work_order, "status": {"$in": list(ACTIVE_STATUSES)}}
    )

    if in_progress_process:
        return JsonResponse(
            {
                "status": "in_progress",
                "in_progress_data": _session_public(in_progress_process),
            }
        )

    # Machine already running a different WO
    if machine_id:
        busy = _find_running_on_machine(db, machine_id)
        if busy and busy.get("work_order") != work_order:
            return JsonResponse(
                {
                    "error": (
                        f"Machine {machine_id} already running Work Order "
                        f"{busy.get('work_order')}. Finish it first."
                    ),
                    "active_session": _session_public(busy),
                },
                status=409,
            )

    sap_line = next((l for l in lines if not l["completed_in_sap"]), None)

    if not sap_line:
        return JsonResponse(
            {
                "error": "This work order is already completed in SAP. Please proceed with next process."
            },
            status=400,
        )

    # If machine_id is provided, validate against PMS_oee_cell
    if machine_id:
        import re

        config = db["PMS_oee_cell"].find_one(
            {"part_name": {"$regex": f"^{re.escape(part_no or '')}$", "$options": "i"}}
        )

        if not config:
            return JsonResponse(
                {"error": f"No PMS configuration found for part '{part_no}'."},
                status=404,
            )

        valid_machine_process = False
        for process_group in config.get("processes", []):
            for proc in process_group.get("process", []):
                if proc.get("description", "").strip() == process_name:
                    for machine in proc.get("machines", []):
                        if machine.get("machineName", "").strip() == machine_id:
                            valid_machine_process = True
                            break
                if valid_machine_process:
                    break
            if valid_machine_process:
                break

        if not valid_machine_process:
            return JsonResponse(
                {
                    "error": f"Machine '{machine_id}' is not configured for process '{process_name}' for this part."
                },
                status=400,
            )

    if has_pcb_trace_completion(db, work_order, machine_id, process_name):
        # Check if there is remaining balance for this specific machine+process
        proc_records = list(
            db["PCB_Trace"].find(
                {
                    "work_order": work_order,
                    "machine_id": machine_id,
                    "process_name": process_name,
                    "status": "COMPLETED",
                }
            )
        )
        proc_total = sum(
            int(r.get("production_count", 0) or 0)
            + int(r.get("rejected_count", 0) or 0)
            for r in proc_records
        )
        # Use woqty stored on the record (original WO qty), not SAP stock_qty
        ref_woqty = int((proc_records[0].get("woqty") or 0) if proc_records else 0)
        if ref_woqty <= 0:
            ref_woqty = int(sap_line.get("stock_qty", 0))
        balance_qty = max(0, ref_woqty - proc_total)
        if balance_qty <= 0:
            return JsonResponse(
                {
                    "error": "This work order is already completed. Proceed with next process."
                },
                status=400,
            )

    part_doc = find_part_document(db, part_no=part_no, material_code=material_code)
    part_name = part_doc.get("part_name", part_no) if part_doc else part_no

    request.session["wip_machine_id"] = machine_id
    request.session["wip_process_name"] = process_name
    request.session["wip_alt_bom"] = alt_bom

    # Calculate remaining balance for this specific machine+process
    proc_records = list(
        db["PCB_Trace"].find(
            {
                "work_order": work_order,
                "machine_id": machine_id,
                "process_name": process_name,
                "status": "COMPLETED",
            }
        )
    )
    total_completed = sum(
        int(r.get("production_count", 0) or 0) + int(r.get("rejected_count", 0) or 0)
        for r in proc_records
    )
    ref_woqty = int((proc_records[0].get("woqty") or 0) if proc_records else 0)
    if ref_woqty <= 0:
        ref_woqty = int(sap_line.get("stock_qty", 0))
    balance_qty = max(0, ref_woqty - total_completed)

    return JsonResponse(
        {
            "status": "success",
            "work_order": work_order,
            "machine_id": machine_id,
            "process_name": process_name,
            "alt_bom": alt_bom,
            "material_code": material_code,
            "part_name": part_name,
            "stock_qty": ref_woqty,
            "completed_qty": total_completed,
            "balance_qty": balance_qty,
            "material_record": sap_line["material_record"],
        }
    )


def scan_machine(request):
    """Backward-compatible alias for validate_machine_process."""
    return validate_machine_process(request)


def get_machine_processes(request):
    """
    Return process descriptions for a part + machine from PMS_oee_cell.
    Called after machine is scanned to populate the process dropdown.
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    plant_code, err = _plant_required(request)
    if err:
        return err

    data = _parse_json_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    machine_id = data.get("machine_id", "").strip()
    part_no = data.get("part_no", "").strip()

    if not machine_id or not part_no:
        return JsonResponse({"error": "machine_id and part_no required"}, status=400)

    db = get_db_connection(plant_code)
    import re

    config = db["PMS_oee_cell"].find_one(
        {"part_name": {"$regex": f"^{re.escape(part_no)}$", "$options": "i"}}
    )

    if not config or "processes" not in config:
        return JsonResponse(
            {"error": f"No config found for part '{part_no}'."},
            status=404,
        )

    descriptions = []
    for process_group in config["processes"]:
        for proc in process_group.get("process", []):
            for machine in proc.get("machines", []):
                if machine.get("machineName", "").strip() == machine_id:
                    desc = proc.get("description", "").strip()
                    if desc and desc not in descriptions:
                        descriptions.append(desc)

    if not descriptions:
        return JsonResponse(
            {"error": f"Machine '{machine_id}' has no processes for part '{part_no}'."},
            status=404,
        )

    return JsonResponse({"status": "success", "processes": descriptions})


def submit_production(request):
    """Submit production to SAP and save PCB_Trace after process started."""
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    plant_code, err = _plant_required(request)
    if err:
        return err

    data = _parse_json_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    mode = data.get("mode", "SUBMIT")  # "START" or "SUBMIT"

    if mode == "START":
        # Handle "Start" button click - create RUNNING record + begin poll
        work_order = data.get("work_order", "").strip()
        machine_id = data.get("machine_id", "").strip()
        process_name = data.get("process_name", "").strip()
        operator_id = data.get("operator_id", "").strip()

        if not all([work_order, machine_id, process_name, operator_id]):
            return JsonResponse(
                {"error": "Missing required fields for START"}, status=400
            )

        db = get_db_connection(plant_code)

        # One RUNNING session per machine
        existing_machine = _find_running_on_machine(db, machine_id)
        if existing_machine:
            active_wo = existing_machine.get("work_order", "")
            return JsonResponse(
                {
                    "error": (
                        f"Machine {machine_id} already has active Work Order "
                        f"{active_wo}. Complete or stop it before starting another."
                    ),
                    "active_session": _session_public(existing_machine),
                },
                status=409,
            )

        if is_polling(machine_id):
            return JsonResponse(
                {
                    "error": (
                        f"Machine {machine_id} already has an active polling task."
                    )
                },
                status=409,
            )

        sap_plant_code = request.session.get(
            "wip_sap_plant_code"
        ) or get_sap_plant_code(db, plant_code)

        sap_pack, err = _fetch_sap_lines(work_order, sap_plant_code)
        if err:
            return err

        result, lines, woqty, _ = sap_pack
        material_code, part_no, summary_qty, _, _ = summarize_sap_work_order(result)
        # Use the SAP WOQTY (original order qty), not IP_STOCKQTY which decreases as qty posts
        display_woqty = int(summary_qty or woqty or 0)

        # Find the specific line for this machine/process to get stock_qty
        sap_line = match_sap_line(lines, machine_id, process_name)
        if not sap_line:
            return JsonResponse(
                {"error": "Machine/Process not found in SAP lines."}, status=400
            )

        # Fresh baseline from current machine API counter (never reuse prior WO)
        try:
            baseline_count = fetch_machine_counter(machine_id)
        except RuntimeError as exc:
            return JsonResponse(
                {"error": f"Cannot read machine counter: {exc}"},
                status=502,
            )

        now = datetime.now()
        doc = {
            "operator_id": operator_id,
            "machine_id": machine_id,
            "work_order": work_order,
            "process_name": process_name,
            "process_selected": process_name,
            "material_code": material_code,
            "part_no": part_no,
            "production_count": 0,
            "rejected_count": 0,
            "woqty": display_woqty,
            "baseline_count": baseline_count,
            "machine_counter": baseline_count,
            "status": "RUNNING",
            "poll_active": True,
            "qty_reached": False,
            "sap_posted": False,
            "start_time": now,
            "timestamp": now,
            "shift": determine_shift(now),
            "plant_code": plant_code,
            "ws_group": machine_group_name(machine_id),
        }
        insert_result = db["PCB_Trace"].insert_one(doc)
        session_id = str(insert_result.inserted_id)
        doc["_id"] = insert_result.inserted_id

        started = start_machine_poller(machine_id, plant_code, session_id)
        if not started:
            # Race: another poller appeared — roll back session
            db["PCB_Trace"].delete_one({"_id": insert_result.inserted_id})
            return JsonResponse(
                {
                    "error": (
                        f"Machine {machine_id} already has an active polling task."
                    )
                },
                status=409,
            )

        traceability_logs(
            request,
            1,
            (
                f"WIP START WO={work_order} machine={machine_id} "
                f"baseline={baseline_count} woqty={display_woqty}"
            ),
        )
        return JsonResponse(
            {
                "status": "success",
                "message": "Process started",
                "session": _session_public(doc),
            }
        )

    # Default mode is "SUBMIT"
    if not data.get("process_started"):
        return JsonResponse(
            {"error": "Press Start before entering production."},
            status=400,
        )

    operator_id = normalize_operator_id(data.get("operator_id", ""))
    machine_id = data.get("machine_id", "").strip()
    alt_bom = data.get("alt_bom", "").strip()
    work_order = data.get("work_order", "").strip() or request.session.get(
        "wip_work_order", ""
    )
    process_name = data.get("process_name", "").strip() or request.session.get(
        "wip_process_name", ""
    )
    production_count = int(data.get("production_count", 0) or 0)
    rejected_count = int(data.get("rejected_count", 0) or 0)
    rejection_details = data.get("rejection_details", [])

    if rejected_count > 0 and not rejection_details:
        return JsonResponse(
            {"error": "Rejection reasons required when rejected_count > 0"},
            status=400,
        )

    if not all([operator_id, machine_id, work_order, process_name]):
        return JsonResponse({"error": "Missing required fields"}, status=400)

    db = get_db_connection(plant_code)
    sap_plant_code = request.session.get("wip_sap_plant_code") or get_sap_plant_code(
        db, plant_code
    )

    sap_pack, err = _fetch_sap_lines(work_order, sap_plant_code)
    if err:
        return err

    result, lines, _, material_plant_list = sap_pack

    # Match line with alt_bom if provided
    sap_line = None
    if alt_bom:
        # Try to find a line matching machine+process+alt_bom first
        alt_lines = [l for l in lines if l.get("alt_bom") == alt_bom]
        sap_line = match_sap_line(alt_lines, machine_id, process_name)
    if not sap_line:
        sap_line = match_sap_line(lines, machine_id, process_name)

    if not sap_line:
        return JsonResponse(
            {"error": "Machine and process no longer valid in SAP."},
            status=400,
        )

    if sap_line["completed_in_sap"]:
        return JsonResponse(
            {
                "error": (
                    "Work order already completed for this process in SAP. "
                    "Cannot save to trace."
                )
            },
            status=400,
        )

    # Check if we are exceeding remaining quantity
    # Scope to the specific machine+process for this WO
    query = {
        "work_order": work_order,
        "machine_id": machine_id,
        "process_name": process_name,
        "status": {"$in": ["COMPLETED", "IN_PROGRESS", "RUNNING"]},
    }
    already_posted = list(db["PCB_Trace"].find(query).sort("timestamp", 1))
    # Only SAP-posted COMPLETED rows count toward remaining balance.
    # Active RUNNING / qty-reached (awaiting SAP) hold the live session count.
    total_posted = sum(
        int(d.get("production_count", 0) or 0) + int(d.get("rejected_count", 0) or 0)
        for d in already_posted
        if d.get("status") == "COMPLETED"
        and (d.get("sap_posted") or d.get("sap_response"))
        and not (d.get("qty_reached") and not d.get("sap_posted"))
    )

    # Use woqty from the existing PCB_Trace record if available — it reflects
    # the original WO quantity, not IP_STOCKQTY which SAP reduces as qty is posted.
    existing_record = already_posted[0] if already_posted else None
    woqty_reference = int(existing_record.get("woqty") or 0) if existing_record else 0
    if woqty_reference <= 0:
        # No prior record — derive from SAP result (WOQTY / summary_qty)
        _, _, sap_summary_qty, _, _ = summarize_sap_work_order(result)
        woqty_reference = int(sap_summary_qty or 0)
    if woqty_reference <= 0:
        # Final fallback: IP_STOCKQTY (should rarely reach here)
        woqty_reference = int(sap_line.get("stock_qty", 0))

    remaining_qty = woqty_reference - total_posted

    if (production_count + rejected_count) > remaining_qty:
        return JsonResponse(
            {
                "error": f"Total (Prod: {production_count} + Rej: {rejected_count} = {production_count + rejected_count}) exceeds remaining quantity ({remaining_qty})."
            },
            status=400,
        )

    # Block only if fully completed with no IN_PROGRESS record to update
    if (
        has_pcb_trace_completion(db, work_order, machine_id, process_name)
        and not any(d.get("status") in ACTIVE_STATUSES for d in already_posted)
        and remaining_qty <= 0
    ):
        return JsonResponse(
            {"error": "This work order is already completed."},
            status=400,
        )

    material_code, part_no, _, _, _ = summarize_sap_work_order(result)
    material_record = sap_line.get("material_record") or (
        material_plant_list[0] if material_plant_list else {}
    )

    # Prefer live session count from DB when present
    active_session = next(
        (
            d
            for d in already_posted
            if d.get("status") in ACTIVE_STATUSES
            or (
                d.get("status") == "COMPLETED"
                and d.get("qty_reached")
                and not d.get("sap_posted")
            )
        ),
        None,
    )
    if active_session:
        live_count = int(active_session.get("production_count", 0) or 0)
        if live_count > 0:
            production_count = live_count
        wo_cap = int(active_session.get("woqty") or woqty_reference or 0)
        if wo_cap > 0:
            production_count = min(production_count, wo_cap)

    stop_machine_poller(machine_id)

    sap_response = post_to_sap_prodent_ot(
        sap_code=sap_plant_code,
        work_order=work_order,
        machine_id=machine_id,
        operator_id=operator_id,
        rejected_count=rejected_count,
        ok_qty=production_count,
        material_record=material_record,
    )

    if not sap_response.get("status"):
        return JsonResponse(
            {
                "error": sap_response.get("message", "SAP posting failed"),
                "sap_response": sap_response,
            },
            status=400,
        )

    now = datetime.now()
    shift = determine_shift(now)

    # Check if we are updating an active / qty-reached session
    in_progress_record = next(
        (
            d
            for d in already_posted
            if d.get("status") in ACTIVE_STATUSES
            or (
                d.get("status") == "COMPLETED"
                and d.get("qty_reached")
                and not d.get("sap_posted")
            )
        ),
        None,
    )

    # The most recent SAP-posted COMPLETED record for this WO+machine+process
    last_completed_record = next(
        (
            d
            for d in reversed(already_posted)
            if d.get("status") == "COMPLETED"
            and (d.get("sap_posted") or d.get("sap_response"))
            and d is not in_progress_record
        ),
        None,
    )

    # Preserve start_time and calculate duration
    anchor_record = in_progress_record or last_completed_record
    start_time = None
    if anchor_record:
        start_time = anchor_record.get("start_time") or anchor_record.get("timestamp")
    duration_seconds = None
    if start_time:
        duration_seconds = max(0, int((now - start_time).total_seconds()))

    doc = {
        "operator_id": operator_id,
        "machine_id": machine_id,
        "work_order": work_order,
        "process_name": process_name,
        "process_selected": process_name,
        "alt_bom": alt_bom,
        "material_code": material_code,
        "part_no": part_no,
        "production_count": production_count,
        "rejected_count": rejected_count,
        "woqty": woqty_reference,
        "status": "COMPLETED",
        "qty_reached": True,
        "sap_posted": True,
        "poll_active": False,
        "sap_response": sap_response,
        "start_time": start_time,
        "end_time": now,
        "duration_seconds": duration_seconds,
        "timestamp": now,
        "shift": shift,
        "plant_code": plant_code,
        "rejection_details": rejection_details,
    }

    if in_progress_record:
        # Promote RUNNING / qty-reached → SAP COMPLETED
        # Keep baseline from original start; do not accumulate twice if qty already set
        if (
            in_progress_record.get("qty_reached")
            or in_progress_record.get("status") in ACTIVE_STATUSES
        ):
            doc["production_count"] = max(
                int(in_progress_record.get("production_count", 0) or 0),
                production_count,
            )
            doc["rejected_count"] = (
                int(in_progress_record.get("rejected_count", 0) or 0) + rejected_count
            )
            doc["baseline_count"] = in_progress_record.get("baseline_count")
            doc["machine_counter"] = in_progress_record.get("machine_counter")
        else:
            doc["production_count"] = (
                int(in_progress_record.get("production_count", 0) or 0)
                + production_count
            )
            doc["rejected_count"] = (
                int(in_progress_record.get("rejected_count", 0) or 0) + rejected_count
            )
        db["PCB_Trace"].replace_one({"_id": in_progress_record["_id"]}, doc)
    elif last_completed_record:
        # Partial top-up: accumulate onto existing COMPLETED record (same _id)
        doc["production_count"] = (
            int(last_completed_record.get("production_count", 0) or 0)
            + production_count
        )
        doc["rejected_count"] = (
            int(last_completed_record.get("rejected_count", 0) or 0) + rejected_count
        )
        # Merge rejection details with existing record
        doc["rejection_details"] = (
            last_completed_record.get("rejection_details", []) + rejection_details
        )
        # Preserve the original start_time from the first submission
        doc["start_time"] = last_completed_record.get(
            "start_time"
        ) or last_completed_record.get("timestamp")
        if doc["start_time"]:
            doc["duration_seconds"] = max(
                0, int((now - doc["start_time"]).total_seconds())
            )
        db["PCB_Trace"].replace_one({"_id": last_completed_record["_id"]}, doc)
    else:
        # Brand-new submission — no prior record
        doc["start_time"] = now
        doc["duration_seconds"] = 0
        db["PCB_Trace"].insert_one(doc)

    traceability_logs(
        request,
        1,
        (
            f"WIP submitted WO={work_order} machine={machine_id} "
            f"process={process_name} qty={production_count}"
        ),
    )
    return JsonResponse(
        {
            "status": "success",
            "message": "Production saved",
            "sap_response": sap_response,
        }
    )


@require_GET
def session_status(request):
    """
    HTTP fallback / resume poll for live production count.
    Query: machine_id=...  (preferred) or session_id=...
    """
    plant_code, err = _plant_required(request)
    if err:
        return err

    machine_id = (request.GET.get("machine_id") or "").strip()
    session_id = (request.GET.get("session_id") or "").strip()
    if not machine_id and not session_id:
        return JsonResponse({"error": "machine_id or session_id required"}, status=400)

    db = get_db_connection(plant_code)
    doc = None
    if session_id:
        try:
            doc = db["PCB_Trace"].find_one({"_id": ObjectId(session_id)})
        except Exception:
            return JsonResponse({"error": "Invalid session_id"}, status=400)
    if doc is None and machine_id:
        doc = _find_running_on_machine(db, machine_id)
        if doc is None:
            doc = db["PCB_Trace"].find_one(
                {
                    "machine_id": machine_id,
                    "status": "COMPLETED",
                    "qty_reached": True,
                    "sap_posted": {"$ne": True},
                },
                sort=[("timestamp", -1)],
            )

    if not doc:
        return JsonResponse(
            {
                "status": "idle",
                "machine_id": machine_id,
                "polling": is_polling(machine_id),
            }
        )

    # Restart poller if session still RUNNING but thread died
    if (
        doc.get("status") in ACTIVE_STATUSES
        and doc.get("poll_active")
        and machine_id
        and not is_polling(machine_id)
    ):
        start_machine_poller(
            machine_id or doc.get("machine_id", ""), plant_code, str(doc["_id"])
        )

    payload = _session_public(doc)
    payload["polling"] = is_polling(payload.get("machine_id") or machine_id)
    payload["event"] = (
        "completed"
        if payload.get("status") == "COMPLETED" and payload.get("qty_reached")
        else "production_update"
    )
    return JsonResponse({"status": "success", **payload})
