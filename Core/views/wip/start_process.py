"""WIP start/complete process API views (work-order-first flow)."""

import json
import logging
import re
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
    Scan work order first — fetch material, quantity, and processes from SAP.
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

    return JsonResponse(
        {
            "status": "success",
            "work_order": work_order,
            "machine_id": machine_id,
            "process_name": process_name,
            "alt_bom": alt_bom,
            "material_code": material_code,
            "part_name": part_name,
            "stock_qty": sap_line["stock_qty"],
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
        material_code, part_no, _, _, _ = summarize_sap_work_order(result)

        # Find the specific line for this machine/process to get stock_qty
        sap_line = next(
            (
                l
                for l in lines
                if l["work_center"] == machine_id
                and l["process_description"] == process_name
            ),
            None,
        )
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
            "woqty": sap_line.get("stock_qty"),
            "status": "IN_PROGRESS",
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
    for line in lines:
        if match_sap_line([line], machine_id, process_name):
            if alt_bom:
                if line.get("alt_bom") == alt_bom:
                    sap_line = line
                    break
            else:
                sap_line = line
                break

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
    query = {
        "work_order": work_order,
        "machine_id": machine_id,
        "process_name": process_name,
        "status": {"$in": ["COMPLETED", "IN_PROGRESS"]},
    }
    already_posted = list(db["PCB_Trace"].find(query))
    total_posted = sum(
        d.get("production_count", 0) + d.get("rejected_count", 0)
        for d in already_posted
    )

    remaining_qty = sap_line.get("stock_qty", 0) - total_posted

    if (production_count + rejected_count) > remaining_qty:
        return JsonResponse(
            {
                "error": f"Total (Prod: {production_count} + Rej: {rejected_count} = {production_count + rejected_count}) exceeds remaining quantity ({remaining_qty})."
            },
            status=400,
        )

    if has_pcb_trace_completion(db, work_order, machine_id, process_name) and not any(
        d.get("status") == "IN_PROGRESS" for d in already_posted
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
        "woqty": sap_line.get("stock_qty"),
        "status": "COMPLETED",
        "sap_response": sap_response,
        "timestamp": now,
        "shift": shift,
        "plant_code": plant_code,
    }

    if in_progress_record:
        # Update existing IN_PROGRESS record to COMPLETED
        db["PCB_Trace"].replace_one({"_id": in_progress_record["_id"]}, doc)
    else:
        # Create new COMPLETED record
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
