"""WIP start/complete process API views."""

import json
import logging
from datetime import datetime

from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt

from Core.auth.logs import traceability_logs
from Core.components.db_connection_string import get_db_connection
from Core.functions.sap_bapi_fetch import fetch_process_details, post_to_sap_prodent_ot
from Core.functions.wip_helpers import (
    calculate_remaining_work_order_quantity,
    find_part_document,
    is_sap_work_order_unavailable,
    is_work_order_closed_in_trace,
)

logger = logging.getLogger(__name__)

PLANT_CODES = ["002", "034", "143", "123", "041"]


def _parse_json_body(request):
    """Return JSON body dict or None if invalid."""
    try:
        return json.loads(request.body)
    except json.JSONDecodeError:
        return None


def _resolve_part_doc(db, data, session):
    """Find part document from request payload and session fallbacks."""
    part_name = (
        (data.get("part_name") or "").strip()
        or session.get("wip_part_name", "")
    )
    material_code = (
        (data.get("material_code") or "").strip()
        or session.get("wip_material_code", "")
    )
    return find_part_document(
        db,
        part_no=part_name,
        part_name=part_name,
        material_code=material_code,
    )


def _validate_work_order_available(db, work_order, process_data, woqty):
    """
    Return (ok, error_message).
    Blocks SAP-closed work orders and locally completed quantities.
    """
    if is_sap_work_order_unavailable(process_data, woqty):
        return False, "Work order not found or already completed in SAP."

    if is_work_order_closed_in_trace(db, work_order, woqty):
        return False, "Work order already completed. Cannot process again."

    remaining = calculate_remaining_work_order_quantity(db, work_order, woqty)
    if remaining <= 0:
        return False, "Work order already completed. Cannot process again."

    return True, ""


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


@csrf_exempt
def scan_part_no(request):
    """Validate scanned part number."""
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    plant_code = request.session.get("plant_code")
    if not plant_code:
        return JsonResponse({"error": "Plant code not in session"}, status=400)
    data = _parse_json_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    part_no = data.get("part_no", "").strip()
    if not part_no:
        return JsonResponse({"error": "Part No required"}, status=400)

    db = get_db_connection(plant_code)
    part_doc = find_part_document(db, part_no=part_no, part_name=part_no)
    if not part_doc:
        return JsonResponse({"error": "Part not found"}, status=400)

    part_name = part_doc.get("part_name", part_no)
    material_code = part_doc.get("material_code") or part_no
    request.session["wip_part_name"] = part_name
    request.session["wip_material_code"] = material_code

    return JsonResponse(
        {
            "status": "success",
            "part_name": part_name,
            "sap_plant_code": part_doc.get("sap_plant_code"),
            "material_code": material_code,
        }
    )


@csrf_exempt
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


@csrf_exempt
def scan_machine(request):
    """Validate machine for part and return process list."""
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    plant_code = request.session.get("plant_code")
    if not plant_code:
        return JsonResponse({"error": "Plant code not in session"}, status=400)
    data = _parse_json_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    machine_id = data.get("machine_id", "").strip()
    part_name = (
        data.get("part_name", "").strip()
        or request.session.get("wip_part_name", "")
    )
    if not machine_id or not part_name:
        return JsonResponse({"error": "Machine ID and Part Name required"}, status=400)

    db = get_db_connection(plant_code)
    config = db["PMS_oee_cell"].find_one(
        {"part_name": {"$regex": f"^{part_name}$", "$options": "i"}},
        {"processes": 1},
    )
    process_list = []
    if config and "processes" in config:
        for pg in config["processes"]:
            for proc in pg.get("process", []):
                if any(
                    m.get("machineName", "").strip() == machine_id
                    for m in proc.get("machines", [])
                ):
                    desc = proc.get("description", "").strip()
                    if desc:
                        process_list.append(desc)
    return JsonResponse({"status": "success", "process_list": process_list})


@csrf_exempt
def scan_work_order(request):
    """Validate work order against SAP and local trace records."""
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    plant_code = request.session.get("plant_code")
    if not plant_code:
        return JsonResponse({"error": "Plant code not in session"}, status=400)
    data = _parse_json_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    work_order = data.get("work_order", "").strip()
    if not work_order:
        return JsonResponse({"error": "Work Order required"}, status=400)

    db = get_db_connection(plant_code)
    part_doc = _resolve_part_doc(db, data, request.session)
    if not part_doc:
        return JsonResponse(
            {"error": "Part not found. Rescan part number first."},
            status=400,
        )

    sap_plant_code = part_doc.get("sap_plant_code")
    if not sap_plant_code:
        return JsonResponse({"error": "SAP Plant Code not found"}, status=400)

    result, bapi_ok, error_msg, woqty, material_plant_list = fetch_process_details(
        work_order,
        sap_plant_code,
    )
    if not bapi_ok or result is None:
        return JsonResponse({"error": error_msg or "SAP fetch failed"}, status=400)

    wo_ok, wo_error = _validate_work_order_available(db, work_order, result, woqty)
    if not wo_ok:
        return JsonResponse({"error": wo_error}, status=400)

    remaining = calculate_remaining_work_order_quantity(db, work_order, woqty)
    return JsonResponse(
        {
            "status": "success",
            "woqty": woqty,
            "remaining_qty": remaining,
            "material_plant_list": material_plant_list,
            "work_order": work_order,
            "material_code": part_doc.get("material_code")
            or request.session.get("wip_material_code", ""),
            "part_name": part_doc.get("part_name"),
        }
    )


@csrf_exempt
def submit_production(request):
    """Submit production entry to SAP and PCB_Trace."""
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)
    plant_code = request.session.get("plant_code")
    if not plant_code:
        return JsonResponse({"error": "Plant code not in session"}, status=400)
    data = _parse_json_body(request)
    if data is None:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    operator_id = normalize_operator_id(data.get("operator_id", ""))
    machine_id = data.get("machine_id", "").strip()
    work_order = data.get("work_order", "").strip()
    production_count = int(data.get("production_count", 0) or 0)
    rejected_count = int(data.get("rejected_count", 0) or 0)

    if not all([operator_id, machine_id, work_order]):
        return JsonResponse({"error": "Missing required fields"}, status=400)

    db = get_db_connection(plant_code)
    part_doc = _resolve_part_doc(db, data, request.session)
    if not part_doc:
        return JsonResponse({"error": "Part not found. Rescan part number."}, status=400)

    material_code = part_doc.get("material_code") or request.session.get(
        "wip_material_code", ""
    )
    sap_plant_code = part_doc.get("sap_plant_code")
    result, bapi_ok, error_msg, woqty, material_plant_list = fetch_process_details(
        work_order,
        sap_plant_code,
    )
    if not bapi_ok or not material_plant_list:
        return JsonResponse({"error": error_msg or "SAP fetch failed"}, status=400)

    wo_ok, wo_error = _validate_work_order_available(db, work_order, result, woqty)
    if not wo_ok:
        return JsonResponse({"error": wo_error}, status=400)

    remaining = calculate_remaining_work_order_quantity(db, work_order, woqty)
    if production_count + rejected_count > remaining:
        return JsonResponse(
            {
                "error": (
                    f"Quantity exceeds remaining WO balance ({remaining}). "
                    "Work order may already be completed."
                )
            },
            status=400,
        )

    material_record = material_plant_list[0] if material_plant_list else {}
    sap_response = post_to_sap_prodent_ot(
        sap_code=sap_plant_code,
        work_order=work_order,
        machine_id=machine_id,
        operator_id=operator_id,
        rejected_count=rejected_count,
        ok_qty=production_count,
        material_record=material_record,
    )
    now = datetime.now()
    shift = determine_shift(now)
    doc = {
        "operator_id": operator_id,
        "machine_id": machine_id,
        "work_order": work_order,
        "material_code": material_code,
        "production_count": production_count,
        "rejected_count": rejected_count,
        "woqty": woqty,
        "status": "COMPLETED" if sap_response.get("status") else "PENDING",
        "sap_response": sap_response,
        "timestamp": now,
        "shift": shift,
        "plant_code": plant_code,
    }
    db["PCB_Trace"].insert_one(doc)
    traceability_logs(
        request,
        1,
        f"WIP production submitted: WO={work_order}, Count={production_count}",
    )
    return JsonResponse(
        {
            "status": "success",
            "message": "Production saved",
            "sap_response": sap_response,
        }
    )
