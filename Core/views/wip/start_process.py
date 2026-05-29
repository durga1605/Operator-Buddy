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
            "wip/start_complete_process.html",
            {"error": "Plant code required. Select a plant first."},
        )
    return render(request, "wip/start_complete_process.html")


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

    _result, lines, woqty, _material_plant_list = sap_pack
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
    Validate machine + process against SAP and check PCB_Trace duplicates.
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    plant_code, err = _plant_required(request)
    if err:
        return err

    data = _parse_json_body(request)
    print("data:", data)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    work_order = data.get("work_order", "").strip() or request.session.get(
        "wip_work_order", ""
    )
    machine_id = data.get("machine_id", "").strip()
    process_name = (
        data.get("process_name", "").strip() or data.get("process_selected", "").strip()
    )

    if not work_order:
        return JsonResponse({"error": "Work Order required"}, status=400)
    if not machine_id:
        return JsonResponse({"error": "Machine ID required"}, status=400)
    if not process_name:
        return JsonResponse({"error": "Process selection required"}, status=400)

    db = get_db_connection(plant_code)
    sap_plant_code = request.session.get("wip_sap_plant_code") or get_sap_plant_code(
        db, plant_code
    )

    sap_pack, err = _fetch_sap_lines(work_order, sap_plant_code)
    if err:
        return err

    result, lines, _woqty, _material_plant_list = sap_pack
    sap_line = match_sap_line(lines, machine_id, process_name)

    if not sap_line:
        return JsonResponse(
            {
                "error": (
                    f"Machine '{machine_id}' is not valid for process "
                    f"'{process_name}' in SAP."
                )
            },
            status=400,
        )

    if sap_line["completed_in_sap"]:
        return JsonResponse(
            {
                "error": (
                    "This work order is already completed for this process in SAP. "
                    "Please proceed with next process."
                )
            },
            status=400,
        )

    if has_pcb_trace_completion(db, work_order, machine_id, process_name):
        return JsonResponse(
            {"error": "This work order is already completed."},
            status=400,
        )

    material_code, part_no, _, _, _ = summarize_sap_work_order(result)
    part_doc = find_part_document(db, part_no=part_no, material_code=material_code)
    part_name = part_doc.get("part_name", part_no) if part_doc else part_no

    request.session["wip_machine_id"] = machine_id
    request.session["wip_process_name"] = process_name

    return JsonResponse(
        {
            "status": "success",
            "work_order": work_order,
            "machine_id": machine_id,
            "process_name": process_name,
            "material_code": material_code,
            "part_name": part_name,
            "stock_qty": sap_line["stock_qty"],
            "material_record": sap_line["material_record"],
        }
    )


def scan_machine(request):
    """Backward-compatible alias for validate_machine_process."""
    return validate_machine_process(request)


def scan_part_no(request):
    """Legacy endpoint — not used in WO-first flow."""
    return JsonResponse(
        {"error": "Scan work order first."},
        status=400,
    )


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

    if not data.get("process_started"):
        return JsonResponse(
            {"error": "Press Start before entering production."},
            status=400,
        )

    operator_id = normalize_operator_id(data.get("operator_id", ""))
    machine_id = data.get("machine_id", "").strip()
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

    result, lines, _woqty, material_plant_list = sap_pack
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

    if has_pcb_trace_completion(db, work_order, machine_id, process_name):
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
    doc = {
        "operator_id": operator_id,
        "machine_id": machine_id,
        "work_order": work_order,
        "process_name": process_name,
        "process_selected": process_name,
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
