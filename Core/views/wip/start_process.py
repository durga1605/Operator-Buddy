"""WIP start/complete process API views (work-order-first flow)."""

import json
import logging
from datetime import datetime

from django.http import JsonResponse
from django.shortcuts import redirect, render

from Core.auth.logs import traceability_logs
from Core.components.db_connection_string import get_db_connection
from Core.functions.sap_bapi_fetch import fetch_process_details, post_to_sap_prodent_ot
from Core.functions.wip_helpers import (
    find_part_document,
    get_sap_plant_code,
    has_pcb_trace_completion,
    match_sap_line,
    parse_sap_work_order_lines,
    summarize_sap_work_order,
)

logger = logging.getLogger(__name__)

PLANT_CODES = ["002", "034", "143", "123", "041"]


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

    # ── Priority 1: active IN_PROGRESS ──────────────────────────────
    in_progress = db["PCB_Trace"].find_one(
        {"work_order": work_order, "status": "IN_PROGRESS"}
    )
    if in_progress:
        start_time = in_progress.get("start_time") or in_progress.get("timestamp")
        elapsed_seconds = 0
        if start_time:
            elapsed_seconds = max(0, int((datetime.now() - start_time).total_seconds()))
        return JsonResponse(
            {
                "status": "in_progress",
                "in_progress_data": {
                    "work_order": in_progress.get("work_order", ""),
                    "operator_id": in_progress.get("operator_id", ""),
                    "machine_id": in_progress.get("machine_id", ""),
                    "process_name": in_progress.get("process_name", ""),
                    "material_code": in_progress.get("material_code", ""),
                    "part_no": in_progress.get("part_no", ""),
                    "woqty": in_progress.get("woqty"),
                    "elapsed_seconds": elapsed_seconds,
                },
            }
        )

    # ── Priority 2: check for partial COMPLETED (balance remaining) ──
    # Find the most recent COMPLETED record for this WO.
    # We group by (machine_id, process_name) and check if any process
    # still has remaining balance.
    completed_records = list(
        db["PCB_Trace"]
        .find({"work_order": work_order, "status": "COMPLETED"})
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
        {"work_order": work_order, "status": "IN_PROGRESS"}
    )

    if in_progress_process:
        return JsonResponse(
            {"status": "in_progress", "in_progress_data": in_progress_process}
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
        # Handle "Start" button click - create IN_PROGRESS record
        work_order = data.get("work_order", "").strip()
        machine_id = data.get("machine_id", "").strip()
        process_name = data.get("process_name", "").strip()
        operator_id = data.get("operator_id", "").strip()

        if not all([work_order, machine_id, process_name, operator_id]):
            return JsonResponse(
                {"error": "Missing required fields for START"}, status=400
            )

        db = get_db_connection(plant_code)
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
            "status": "IN_PROGRESS",
            "start_time": now,
            "timestamp": now,
            "shift": determine_shift(now),
            "plant_code": plant_code,
        }
        db["PCB_Trace"].insert_one(doc)
        return JsonResponse({"status": "success", "message": "Process started"})

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
        "status": {"$in": ["COMPLETED", "IN_PROGRESS"]},
    }
    already_posted = list(db["PCB_Trace"].find(query).sort("timestamp", 1))
    total_posted = sum(
        d.get("production_count", 0) + d.get("rejected_count", 0)
        for d in already_posted
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
        and not any(d.get("status") == "IN_PROGRESS" for d in already_posted)
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

    # Check if we are updating an IN_PROGRESS record
    in_progress_record = next(
        (d for d in already_posted if d.get("status") == "IN_PROGRESS"), None
    )

    # The most recent COMPLETED record for this WO+machine+process (for partial top-up)
    last_completed_record = next(
        (d for d in reversed(already_posted) if d.get("status") == "COMPLETED"), None
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
        "sap_response": sap_response,
        "start_time": start_time,
        "end_time": now,
        "duration_seconds": duration_seconds,
        "timestamp": now,
        "shift": shift,
        "plant_code": plant_code,
    }

    if in_progress_record:
        # Promote IN_PROGRESS → COMPLETED, accumulate counts
        doc["production_count"] = (
            int(in_progress_record.get("production_count", 0) or 0) + production_count
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
