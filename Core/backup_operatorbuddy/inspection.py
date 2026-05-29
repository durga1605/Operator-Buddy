"""
Views for handling inspection operations in the Mobility module.
"""

from datetime import datetime, time
import logging
import json
import re
import traceback
import requests
import pytz
from bson import ObjectId
from django.shortcuts import render
from django.http import JsonResponse
from django.conf import settings
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.shortcuts import redirect
from Core.components.db_connection_string import get_db_connection
from Core.auth.logs import traceability_logs
from Core.backup_operatorbuddy.inspection_utility import (
    get_total_work_orders,
    validate_process_sequence,
)
from Core.functions.sap_bapi_fetch import fetch_process_details

logger = logging.getLogger(__name__)


def calculate_used_work_order_quantity(trace_docs):
    """Calculate used quantity from completed PCB_Trace records."""
    used = 0
    for doc in trace_docs:
        print(doc.get("status"), "TRACE DOC STATUS IN CALCULATE USED QUANTITY")
        status = (doc.get("status") or "").upper()
        if status in ("COMPLETED", "OK"):
            ok_qty = doc.get("ok_qty")
            if ok_qty is None:
                ok_qty = doc.get("production_count", 0)
            if ok_qty is None:
                ok_qty = doc.get("work_order_count", 0)
            used += int(float(ok_qty or 0))
            used += int(float(doc.get("rejected_count", 0) or 0))
            used += int(float(doc.get("setup_count", 0) or 0))
    return used


def calculate_remaining_work_order_quantity(db, work_order, total_qty):
    """Calculate remaining quantity for a work order based on total quantity and used quantity."""
    trace_docs = list(db["PCB_Trace"].find({"work_order": work_order}))
    used_qty = calculate_used_work_order_quantity(trace_docs)
    return max(0, int(float(total_qty or 0)) - used_qty)


def normalize_operator_id(op):
    """Normalize operator ID by stripping whitespace and converting to uppercase."""
    return str(op).strip()


def inspection_process(request):
    """
    Render the inspection process selection page.
    """
    plant_code = ""
    if not plant_code:
        plant_code = request.session.get("plant_code")

    if not plant_code:
        # Cannot log without knowing which DB to use
        raise ValueError("Plant code is required for logging traceability")
    db = get_db_connection(plant_code)
    collection = db["machine_process"]
    pipeline = [
        {"$project": {"_id": 0, "project_id": {"$toString": "$_id"}, "process_name": 1}}
    ]
    process_list = list(collection.aggregate(pipeline))
    return render(
        request, "Inspection/inspectionprocess.html", {"process_list": process_list}
    )


def inspection_machine(request):
    """
    Handle inspection machine selection.
    """
    if request.method == "POST":
        data = json.loads(request.body)
        process_name = data.get("process_name")
        if not process_name:
            return JsonResponse({"error": "Process Name is required"}, status=400)
        plant_code = ""
        if not plant_code:
            plant_code = request.session.get("plant_code")

        if not plant_code:
            # Cannot log without knowing which DB to use
            raise ValueError("Plant code is required for logging traceability")
        db = get_db_connection(plant_code)
        collection = db["PMS_oee_cell"]
        result = collection.find_one(
            {"part_name": process_name}, {"_id": 1, "part_name": 1}
        )
        if not result:
            return JsonResponse({"error": "Process not found"}, status=404)
        process_name = result["part_name"]
        process_id = str(result["_id"])
        return JsonResponse({"data": [{"project_id": process_id}]})

    if request.method == "GET":
        process_id = request.GET.get("process_id")
        plant_code = ""
        if not plant_code:
            plant_code = request.session.get("plant_code")

        if not plant_code:
            # Cannot log without knowing which DB to use
            raise ValueError("Plant code is required for logging traceability")
        db = get_db_connection(plant_code)
        collection = db["PMS_oee_cell"]
        response = collection.find_one({"_id": ObjectId(process_id)}, {"part_name": 1})
        process_name = response["part_name"]
        if not process_id:
            return render(
                request,
                "Inspection/inspectionmachine.html",
                {"error_message": "Invalid Process Name"},
            )
        return render(
            request,
            "Inspection/inspectionmachine.html",
            {"process_id": process_id, "process_name": process_name},
        )
    return None


def validate_machine(request):
    """
    Validate if a machine exists for the given machine ID and part (process name).
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        data = json.loads(request.body)
        machine_id = data.get("machine_id", "").strip()
        process_name = data.get("process_name", "").strip()

        if not machine_id or not process_name:
            traceability_logs(
                request,
                2,
                "Machine ID and Process Name are,"
                f"required in WIP inspection. ({process_name}, {machine_id})",
            )
            return JsonResponse(
                {"error": "Machine ID and Process Name are required"}, status=400
            )

        plant_code = ""
        if not plant_code:
            plant_code = request.session.get("plant_code")

        if not plant_code:
            # Cannot log without knowing which DB to use
            raise ValueError("Plant code is required for logging traceability")
        db = get_db_connection(plant_code)
        collection = db["PMS_oee_cell"]
        document = collection.find_one({"part_name": process_name})

        if not document:
            traceability_logs(
                request,
                2,
                f"Part not found in WIP inspection. ({process_name})",
            )
            return JsonResponse(
                {"status": "invalid", "message": "Part not found"}, status=404
            )

        found = False
        for process_group in document.get("processes", []):
            for proc in process_group.get("process", []):
                for machine in proc.get("machines", []):
                    if machine.get("machineName", "").strip() == machine_id:
                        found = True
                        break
                if found:
                    break
            if found:
                break

        if found:
            traceability_logs(
                request,
                1,
                f"Machine '{machine_id}' exists in process '{process_name}'.",
            )
            return JsonResponse({"status": "valid", "message": "Machine exists"})

        traceability_logs(
            request,
            2,
            f"Machine '{machine_id}' not found for part '{process_name}' in WIP inspection.",
        )
        return JsonResponse(
            {"status": "invalid", "message": "Machine not found for given part"},
            status=404,
        )

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


def determine_shift(current_dt):
    """
    Determine the shift based on the current time.
    """
    current_time = current_dt.time()
    shift_1_start = time(8, 0)
    shift_1_end = time(16, 30)
    shift_2_start = time(16, 30)
    shift_2_end = time(1, 0)
    shift_3_start = time(1, 0)
    shift_3_end = time(8, 0)
    if shift_1_start <= current_time < shift_1_end:
        return "Shift 1"
    if current_time >= shift_2_start or current_time < shift_2_end:
        return "Shift 2"
    if shift_3_start <= current_time < shift_3_end:
        return "Shift 3"
    return "Unknown"


def get_machine_description(request):
    """Get machine descriptions for a given part and machine ID."""
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request method."}, status=405)

    try:
        plant_code = ""
        if not plant_code:
            plant_code = request.session.get("plant_code")

        if not plant_code:
            # Cannot log without knowing which DB to use
            raise ValueError("Plant code is required for logging traceability")
        db = get_db_connection(plant_code)
        data = json.loads(request.body.decode("utf-8"))

        part_name = data.get("process_name")
        machine_id = data.get("machine_id")

        if not all([part_name, machine_id]):
            return JsonResponse(
                {"error": "Missing part_name or machine_id."}, status=400
            )

        # Fetch the process config
        config = db["PMS_oee_cell"].find_one({"part_name": part_name})

        if not config or "processes" not in config:
            return JsonResponse({"error": "Part config not found."}, status=404)

        matching_descriptions = []
        is_checked_status = None

        for process_group in config["processes"]:
            for proc in process_group.get("process", []):
                for machine in proc.get("machines", []):
                    if machine.get("machineName", "").strip() == machine_id.strip():
                        desc = proc.get("description", "").strip()
                        is_checked = proc.get(
                            "isChecked", 0
                        )  # Default to 1 if not found

                        if desc and desc not in matching_descriptions:
                            matching_descriptions.append(desc)
                            # Store the isChecked status for the first matching description
                            if is_checked_status is None:
                                is_checked_status = is_checked

        if not matching_descriptions:
            return JsonResponse(
                {"error": "This process is not available at this location."}, status=404
            )

        # Return both the description and the isChecked status
        return JsonResponse(
            {"descriptions": matching_descriptions, "is_checked": is_checked_status}
        )

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


def check_work_order(request):
    """Check if a work order exists for a given machine and part."""
    plant_code = request.session.get("plant_code", "")
    if not plant_code:
        raise ValueError("Plant code is required for logging traceability")

    db = get_db_connection(plant_code)
    partno_collection = db["PMS_partno"]
    collection = db["PCB_Trace"]
    quality_feeder = db["Quality_Feeder"]

    if request.method == "POST":
        try:
            data = json.loads(request.body)
            work_order = data.get("work_order")
            machine_id = data.get("machine_id")
            part_name = data.get("processName")

            if not work_order or not machine_id or not part_name:
                return JsonResponse(
                    {"error": ("Missing work_order, machine_id, or processName")},
                    status=400,
                )

            part_doc = partno_collection.find_one({"part_name": part_name})
            sap_plant_code = part_doc.get("sap_plant_code") if part_doc else None

            existing_docs = list(collection.find({"work_order": work_order}))
            pending_qty_feeder = quality_feeder.find_one(
                {
                    "work_order": work_order,
                    "machine_id": machine_id,
                    "part_name": part_name,
                    "status": {"$regex": "^PENDING$", "$options": "i"},
                }
            )

            if pending_qty_feeder:
                return JsonResponse(
                    {
                        "error": (
                            "Work order has not been cleared from the "
                            "quality feeder for this process. Kindly clear it "
                            "before proceeding."
                        )
                    },
                    status=400,
                )

            # -----------------------------
            # If work order already exists
            # -----------------------------
            if existing_docs:
                if not sap_plant_code:
                    return JsonResponse(
                        {"error": ("SAP Plant Code not found for part")},
                        status=400,
                    )

                (
                    result,
                    status,
                    error_msg,
                    woqty,
                    material_plant_list,
                ) = fetch_process_details(
                    work_order,
                    sap_plant_code,
                )

                if not status or result is None:
                    return JsonResponse(
                        {"error": (error_msg or "SAP fetch failed.")},
                        status=400,
                    )

                total_rejected = 0
                for doc in existing_docs:
                    if doc.get("flag") == 0:
                        total_rejected += doc.get("rejected_count", 0)

                remaining = calculate_remaining_work_order_quantity(
                    db,
                    work_order,
                    woqty,
                )
                print(remaining, "REMAINING WORK ORDER QUANTITY")
                print(
                    db,
                    work_order,
                    woqty,
                    "DB AND WORK ORDER AND WOQTY IN CHECK WORK ORDER",
                )

                if remaining <= 0:
                    return JsonResponse(
                        {"error": "This work order is already scanned."}, status=400
                    )

                return JsonResponse(
                    {
                        "exists": True,
                        "work_order_count": remaining,
                        "rejected_count": total_rejected,
                        "status": "OK",  # Assuming it's okay if no pending status
                    }
                )

            # -----------------------------
            # If work order NOT in PCB_Trace
            # -----------------------------
            if not sap_plant_code:
                return JsonResponse(
                    {"error": ("SAP Plant Code not found for part")},
                    status=400,
                )

            (
                result,
                status,
                error_msg,
                woqty,
                material_plant_list,
            ) = fetch_process_details(
                work_order,
                sap_plant_code,
            )

            if not status or result is None:
                return JsonResponse(
                    {"error": (error_msg or "SAP fetch failed.")},
                    status=400,
                )

            return JsonResponse(
                {
                    "exists": False,
                    "work_order_count": int(float(woqty)),
                    "rejected_count": 0,
                    "woqty": woqty,
                    "material_plant_list": material_plant_list,
                }
            )

        except Exception as exc:
            return JsonResponse(
                {"error": str(exc)},
                status=500,
            )
    return JsonResponse({"error": "Invalid request method."}, status=405)


def submit_inspection(request):
    """
    Submit inspection data for a work order.
    """

    if request.method != "POST":
        return JsonResponse(
            {"error": "Invalid request method"},
            status=405,
        )

    plant_code = ""
    if not plant_code:
        plant_code = request.session.get("plant_code")

    if not plant_code:
        raise ValueError("Plant code is required for logging traceability")

    db = get_db_connection(plant_code)
    collection = db["PCB_Trace"]

    try:
        data = json.loads(request.body.decode("utf-8"))

        required_fields = [
            "operator_id",
            "machine_id",
            "work_order",
            "workorder_count",
            "processName",
            "machine_description",
        ]

        for field in required_fields:
            print(f"Field {field}: {repr(data.get(field))}")

        if not all(data.get(field) for field in required_fields):
            return JsonResponse(
                {"error": "Missing required fields"},
                status=400,
            )

        operator_id = normalize_operator_id(data.get("operator_id"))
        machine_id = data["machine_id"]
        work_order = data["work_order"]
        work_order_counts = int(data["workorder_count"])
        process_name = data["processName"]
        machine_description = data["machine_description"]
        fpd_process = data.get("fpd_process", False)
        scan_tool = data.get("scan_tool")
        scan_coil = data.get("scan_coil")

        if fpd_process:
            if not scan_tool or not scan_coil:
                return JsonResponse(
                    {"error": "FPD Tool and Coil scan required"},
                    status=400,
                )

        now = datetime.now(pytz.timezone(settings.TIME_ZONE))
        shift = determine_shift(now)
        today_date = now.date()

        # PROCESS SEQUENCE VALIDATION
        is_valid, error_msg = validate_process_sequence(
            db,
            work_order,
            machine_id,
            machine_description,
            scanned_part_name=process_name,
        )

        if not is_valid:
            traceability_logs(
                request,
                2,
                "Process validation failed: "
                + f"{error_msg} "
                + f"[Work Order: {work_order}, "
                + f"Process: {process_name}]",
            )
            return JsonResponse(
                {"error": error_msg},
                status=400,
            )

        partno_collection = db["PMS_partno"]
        part_doc = partno_collection.find_one({"part_name": process_name})
        sap_plant_code = part_doc.get("sap_plant_code") if part_doc else None

        if not sap_plant_code:
            return JsonResponse(
                {"error": "SAP Plant Code not found for part"},
                status=400,
            )

        (
            _,
            status,
            error_msg,
            woqty,
            material_plant_list,
        ) = fetch_process_details(
            work_order,
            sap_plant_code,
        )

        print(material_plant_list, "material_plant_list")
        print(status, "status")

        if not status or not material_plant_list:
            return JsonResponse(
                {"error": error_msg or "SAP fetch failed."},
                status=400,
            )

        existing_docs = list(collection.find({"work_order": work_order}))
        used_ok = sum(
            doc.get("workorder_count", 0)
            for doc in existing_docs
            if doc.get("status") == "ok"
        )
        remaining = woqty - used_ok

        if work_order_counts > remaining:
            return JsonResponse(
                {
                    "error": (
                        "Enter within this balance qty. "
                        f"Remaining quantity is {remaining}."
                    )
                },
                status=400,
            )

        expected_process_step = get_expected_process_step(
            db,
            work_order,
            process_name,
        )

        print(expected_process_step, "EXPECTED PROCESS")

        if not expected_process_step:
            return JsonResponse(
                {"error": "Unable to determine expected process step."},
                status=400,
            )

        sap_steps = [
            (m.get("OP_DESCRIPTION") or "").strip() for m in material_plant_list
        ]

        print("SAP STEPS:", sap_steps)
        print("EXPECTED:", expected_process_step)

        material_record = next(
            (
                m
                for m in material_plant_list
                if (m.get("OP_DESCRIPTION") or "").strip() == expected_process_step
            ),
            None,
        )

        if not material_record:
            print("SAP mismatch — allowing due to routing difference")
            material_record = material_plant_list[0]

        if not material_record:
            print("No SAP material found — allowing with fallback")
            material_record = {"MATERIAL": "UNKNOWN"}

        material_code = material_record.get(
            "MATERIAL",
            "UNKNOWN",
        )
        material_plant = material_record["PLANT"]

        # FPD API VALIDATION
        if fpd_process:
            try:
                FPD_API_URL = settings.FPD_API_URL

                fpd_payload = {
                    "work_order": work_order,
                    "machine_id": machine_id,
                    "process_name": process_name,
                    "tool": scan_tool,
                    "coil": scan_coil,
                    "operator_id": operator_id,
                }

                fpd_response = requests.post(
                    FPD_API_URL,
                    json=fpd_payload,
                    timeout=10,
                )

                if fpd_response.status_code != 200:
                    return JsonResponse(
                        {"error": "FPD API connection failed."},
                        status=400,
                    )

                fpd_result = fpd_response.json()

                if not fpd_result.get("status"):
                    return JsonResponse(
                        {
                            "error": fpd_result.get(
                                "message",
                                "FPD validation failed.",
                            )
                        },
                        status=400,
                    )

            except Exception as api_err:
                logging.error(
                    "FPD API Error: %s",
                    str(api_err),
                )
                return JsonResponse(
                    {"error": "FPD API validation error."},
                    status=500,
                )

        # Duplicate same shift
        existing_same_shift = collection.find_one(
            {
                "work_order": work_order,
                "machine_id": machine_id,
                "machine_description": machine_description,
                "shift": shift,
                "timestamp": {
                    "$gte": datetime.combine(
                        today_date,
                        datetime.min.time(),
                    ).astimezone(pytz.timezone(settings.TIME_ZONE)),
                    "$lte": datetime.combine(
                        today_date,
                        datetime.max.time(),
                    ).astimezone(pytz.timezone(settings.TIME_ZONE)),
                },
            }
        )

        datas = {
            "operator_id": operator_id,
            "part_name": process_name,
            "machine_description": machine_description,
            "material_code": material_code,
            "sap_plant_code": material_plant,
            "machine_id": machine_id,
            "work_order": work_order,
            "work_order_count": work_order_counts,
            "start_time": datetime.now(),
            "timestamp": datetime.now(),
            "shift": shift,
            "flag": 0,
            "fpd_process": fpd_process,
            "scan_tool": scan_tool if fpd_process else None,
            "scan_coil": scan_coil if fpd_process else None,
        }

        if existing_same_shift:
            collection.update_one(
                {"_id": existing_same_shift["_id"]},
                {"$set": datas},
            )
            traceability_logs(
                request,
                1,
                "Inspection updated successfully into "
                + "Quality Feeder collection: "
                + f"{process_name} for {work_order}",
            )
        else:
            collection.insert_one(datas)

        traceability_logs(
            request,
            1,
            "Inspection saved successfully into "
            "Quality Feeder collection: " + f"{process_name} for {work_order}",
        )

        total_work_orders = get_total_work_orders(
            collection,
            machine_id,
        )

        return JsonResponse(
            {
                "message": "Inspection saved successfully!",
                "total_work_orders": total_work_orders,
                "shift": shift,
            }
        )

    except json.JSONDecodeError:
        return JsonResponse(
            {"error": "Invalid JSON data"},
            status=400,
        )
    except Exception as e:
        logging.critical(
            "Unexpected error: %s\n%s",
            str(e),
            traceback.format_exc(),
        )
        return JsonResponse(
            {"error": "An unexpected server error occurred."},
            status=500,
        )


def get_expected_process_step(db, work_order, part_name):
    """
    Returns the NEXT expected process description
    """

    # Get trace history for this work order and part
    trace_filter = {"work_order": work_order}
    if part_name:
        trace_filter["part_name"] = {
            "$regex": f"^{re.escape(part_name)}$",
            "$options": "i",
        }

    trace_entries = list(db["PCB_Trace"].find(trace_filter).sort("timestamp", 1))

    # Fetch process config
    config = db["PMS_oee_cell"].find_one(
        {
            "part_name": {"$regex": f"^{re.escape(part_name)}$", "$options": "i"},
            "processes.process.machines": {"$exists": True},
        }
    )

    if not config:
        return None

    process_list = config["processes"][0]["process"]

    # Build unchecked steps
    unchecked_steps = []
    for i, proc in enumerate(process_list):
        if proc.get("isChecked", 0) == 0:
            unchecked_steps.append(
                {
                    "index": i,
                    "description": proc.get("description", "").strip(),
                }
            )

    if not unchecked_steps:
        return None

    # FIRST SCAN
    if not trace_entries:
        return unchecked_steps[0]["description"]

    # LAST COMPLETED PROCESS (by description)
    last_completed_desc = trace_entries[-1].get("machine_description", "").strip()

    last_index = -1
    for i, proc in enumerate(process_list):
        if proc.get("description", "").strip() == last_completed_desc:
            last_index = i
            break

    if last_index == -1:
        return None

    # NEXT unchecked step
    for step in unchecked_steps:
        if step["index"] > last_index:
            return step["description"]

    return None


def inspection_process_for_rejection(request):
    """
    Render the inspection rejection process selection page.
    """
    plant_code = ""
    if not plant_code:
        plant_code = request.session.get("plant_code")

    if not plant_code:
        # Cannot log without knowing which DB to use
        raise ValueError("Plant code is required for logging traceability")
    db = get_db_connection(plant_code)
    collection = db["machine_process"]
    pipeline = [
        {"$project": {"_id": 0, "project_id": {"$toString": "$_id"}, "process_name": 1}}
    ]
    process_list = list(collection.aggregate(pipeline))
    return render(
        request,
        "Inspection/inspectionrejectionprocess.html",
        {"process_list": process_list},
    )


def inspection_rejected(request):
    """
    Handle rejected inspection process selection.
    """
    if request.method == "POST":
        data = json.loads(request.body)
        process_name = data.get("process_name")
        if not process_name:
            return JsonResponse({"error": "Process Name is required"}, status=400)
        plant_code = ""
        if not plant_code:
            plant_code = request.session.get("plant_code")

        if not plant_code:
            raise ValueError("Plant code is required for logging traceability")
        db = get_db_connection(plant_code)
        collection = db["PMS_oee_cell"]
        result = collection.find_one(
            {"part_name": process_name}, {"_id": 1, "part_name": 1}
        )
        if not result:
            traceability_logs(
                request,
                2,
                f"Process not found.{result}",
            )
            return JsonResponse({"error": "Process not found"}, status=404)
        process_name = result["part_name"]
        process_id = str(result["_id"])
        return JsonResponse({"data": [{"project_id": process_id}]})

    if request.method == "GET":
        process_id = request.GET.get("process_id")
        plant_code = ""
        if not plant_code:
            plant_code = request.session.get("plant_code")

        if not plant_code:
            # Cannot log without knowing which DB to use
            raise ValueError("Plant code is required for logging traceability")
        db = get_db_connection(plant_code)
        collection = db["PMS_oee_cell"]
        response = collection.find_one({"_id": ObjectId(process_id)}, {"part_name": 1})
        process_name = response["part_name"]
        if not process_id:
            traceability_logs(
                request,
                2,
                f"Invalid Process Name.{process_id}",
            )
            return render(
                request,
                "Inspection/inspectionreject.html",
                {"error_message": "Invalid Process Name"},
            )
        return render(
            request,
            "Inspection/inspectionreject.html",
            {"process_id": process_id, "process_name": process_name},
        )
    return None


def get_work_order_count(request):
    """
    Get the work order count for a given machine and work order.
    """
    if request.method == "POST":
        try:
            data = json.loads(request.body.decode("utf-8"))
            machine_id = data.get("machine_id")
            work_order = data.get("work_order")
            print(
                work_order,
                machine_id,
                "WORK ORDER AND MACHINE ID IN GET WORK ORDER COUNT",
            )
            if not machine_id or not work_order:
                traceability_logs(
                    request,
                    2,
                    f"Missing machine_id or work_order.{machine_id}{work_order}",
                )
                return JsonResponse(
                    {"error": "Missing machine_id or work_order"}, status=400
                )

            plant_code = ""
            if not plant_code:
                plant_code = request.session.get("plant_code")

            if not plant_code:
                raise ValueError("Plant code is required for logging traceability")

            db = get_db_connection(plant_code)
            collection = db["PCB_Trace"]

            existing_doc = collection.find_one(
                {"work_order": work_order, "machine_id": machine_id}
            )
            print(existing_doc, "EXISTING DOC IN GET WORK ORDER COUNT")
            if not existing_doc:
                return JsonResponse(
                    {"error": "Work order not found for this machine"}, status=404
                )

            sap_plant_code = existing_doc.get("sap_plant_code")
            if not sap_plant_code:
                return JsonResponse(
                    {"error": "SAP Plant Code not found in existing record"},
                    status=400,
                )

            (
                result,
                status,
                error_msg,
                woqty,
                material_plant_list,
            ) = fetch_process_details(
                work_order,
                sap_plant_code,
            )

            print(result, "SAP RESULT in get_work_order_count")

            if not status or result is None:
                return JsonResponse(
                    {"error": error_msg or "SAP fetch failed."}, status=400
                )

            # ------------------ NEW LOGIC USING IP_STOCKQTY ------------------
            ip_stock_qty = 0

            for row in result:
                try:
                    qty = float(row.get("IP_STOCKQTY", 0))
                    if qty > 0:
                        ip_stock_qty = qty
                        break
                except (TypeError, ValueError):
                    continue

            # If no IP stock → Work order completed for this stage
            if ip_stock_qty <= 0:
                return JsonResponse(
                    {
                        "error": "This work order is already completed"
                        " for this process (No IP stock)."
                    },
                    status=400,
                )
            # ----------------------------------------------------------------

            # remaining = calculate_remaining_work_order_quantity(
            #     db,
            #     work_order,
            #     woqty,
            # )

            # if remaining <= 0:
            #     return JsonResponse(
            #         {"error": "This work order is already completed."}, status=400
            #     )

            # Return IP stock as work order count instead of WOQTY balance
            return JsonResponse({"work_order_count": ip_stock_qty})

        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON data"}, status=400)
        except Exception as e:
            logging.critical("Unexpected error: %s\n%s", str(e), traceback.format_exc())
            return JsonResponse(
                {"error": "An unexpected server error occurred."}, status=500
            )

    return JsonResponse({"error": "Invalid request method"}, status=405)


def get_work_order_qty(request):
    """Get the total quantity for a given work order from SAP."""

    plant_code = ""
    if not plant_code:
        plant_code = request.session.get("plant_code")

    db = get_db_connection(plant_code)
    sapplantcode = db["sapplant"]
    if not plant_code:
        raise ValueError("Plant code is required for logging traceability")

    if request.method != "POST":
        return JsonResponse({"error": "Invalid request"}, status=400)

    sap_code_doc = sapplantcode.find_one({"plant_code": int(plant_code)})
    sap_code = sap_code_doc.get("sap_plant_code") if sap_code_doc else None

    data = json.loads(request.body)
    work_order = data.get("work_order")

    if not work_order:
        return JsonResponse({"error": "Work order missing"}, status=400)

    status, error_msg, woqty = fetch_process_details(work_order, sap_code)

    # ❌ If SAP call failed
    if not status:
        return JsonResponse(
            {"error": error_msg or "SAP fetch failed."},
            status=400,
        )

    # ❌ If work order not found (IMPORTANT FIX)
    if not woqty or woqty == 0:
        return JsonResponse(
            {"error": "Work order not found"},
            status=404,
        )

    # ✅ Success
    return JsonResponse({"work_order_qty": woqty})


@csrf_exempt
def submit_rejected_inspection(request):
    """Submit rejected inspection data for a work order."""
    plant_code = request.session.get("plant_code")
    if not plant_code:
        return JsonResponse(
            {"status": False, "message": "Plant code is required"}, status=400
        )

    if request.method != "POST":
        return JsonResponse(
            {"status": False, "message": "Invalid request method"}, status=405
        )

    try:
        data = json.loads(request.body.decode("utf-8"))

        operator_id = normalize_operator_id(data.get("operator_id"))
        machine_id = data.get("machine_id")
        machine_description = (data.get("machine_description") or "").strip()
        work_order = data.get("work_order")
        process_name = data.get("process_name")

        rejected_count = int(data.get("rejected_count", 0))
        rejection_details = data.get("rejection_details", [])
        setup_count = int(data.get("setup_count", 0))
        production_count = int(data.get("production_count", 0))

        shift = determine_shift(datetime.now())

        if not operator_id or not machine_id or not work_order:
            return JsonResponse(
                {"status": False, "message": "Missing required fields"}, status=400
            )

        db = get_db_connection(plant_code)
        pcb_trace = db["PCB_Trace"]
        feeder_col = db["Quality_Feeder"]
        sapplant = db["sapplant"]

        sap_doc = sapplant.find_one({"plant_code": int(plant_code)})
        sap_code = sap_doc.get("sap_plant_code") if sap_doc else None

        result, status, error_msg, woqty, material_list = fetch_process_details(
            work_order, sap_code
        )

        print(
            result,
            status,
            error_msg,
            woqty,
            material_list,
            "SAP RESULT IN REJECTED INSPECTION",
        )
        if not status or result is None:
            return JsonResponse(
                {"status": False, "message": error_msg or "SAP fetch failed."},
                status=400,
            )

        # -------- IP STOCK CHECK --------
        ip_stock_qty = 0
        for row in result:
            try:
                qty = float(row.get("IP_STOCKQTY", 0))
                if qty > 0:
                    ip_stock_qty = qty
                    break
            except Exception:
                pass

        if ip_stock_qty <= 0:
            return JsonResponse(
                {
                    "status": False,
                    "message": "Work order already completed for this process (No IP stock).",
                },
                status=400,
            )

        material_record = next(
            (
                m
                for m in (material_list or [])
                if (m.get("OP_DESCRIPTION") or "").strip() == machine_description
            ),
            material_list[0] if material_list else {},
        )

        ok_qty = production_count

        # # ---------------- SAP CALL ----------------
        # sap_result = post_to_sap_prodent_ot(
        #     call_bapi=call_bapi,
        #     sap_code=sap_code,
        #     work_order=work_order,
        #     machine_id=machine_id,
        #     machine_description=machine_description,
        #     operator_id=operator_id,
        #     rejected_count=rejected_count,
        #     ok_qty=ok_qty,
        #     material_record=material_record
        # )

        # # ---------------- PROPER SAP VALIDATION ----------------
        # sap_data = sap_result.get("data", {})
        # sap_return = sap_data.get("RETURN", {})
        # it_error = sap_data.get("IT_ERROR1", [])

        # # Priority 1: Check IT_ERROR1
        # if it_error and it_error[0].get("TYPE") == "E":
        #     return JsonResponse({
        #         "status": False,
        #         "message": it_error[0].get("MESSAGE")
        #     }, status=400)

        # # Priority 2: Check RETURN
        # if sap_return.get("TYPE") == "E":
        #     return JsonResponse({
        #         "status": False,
        #         "message": sap_return.get("MESSAGE")
        #     }, status=400)

        # # Success message from SAP
        # sap_message = sap_return.get("MESSAGE", "WO Posted To SAP Successfully")

        # ---------------- DB SAVE ----------------
        existing_doc = pcb_trace.find_one(
            {
                "work_order": work_order,
                "machine_id": machine_id,
                "machine_description": machine_description,
                "shift": shift,
            },
            sort=[("timestamp", -1)],
        )

        if rejected_count > 0:
            feeder_data = {
                "machine_id": machine_id,
                "machine_description": machine_description,
                "work_order": work_order,
                "part_name": process_name,
                "operator_id": operator_id,
                "rejected_count": rejected_count,
                "rejection_details": rejection_details,
                "ok_qty": ok_qty,
                "production_count": production_count,
                "setup_count": setup_count,
                "wo_qty": int(woqty),
                "partial_qty": production_count,
                "status": "PENDING",
                "timestamp": datetime.now(),
                "plant_code": plant_code,
                "sap_plant_code": sap_code,
                "shift": shift,
                "material_code": material_record.get("MATERIAL", ""),
            }
            feeder_col.insert_one(feeder_data)

        else:
            inspection_data = {
                "completed_time": datetime.now(),
                "ok_qty": ok_qty,
                "production_count": production_count,
                "setup_count": setup_count,
                "status": "COMPLETED",
                "shift": shift,
            }

            if existing_doc:
                pcb_trace.update_one(
                    {"_id": existing_doc["_id"]},
                    {"$set": inspection_data},
                )
            else:
                inspection_data.update(
                    {
                        "work_order": work_order,
                        "machine_id": machine_id,
                        "machine_description": machine_description,
                        "timestamp": datetime.now(),
                    }
                )
                pcb_trace.insert_one(inspection_data)

        # ✅ Final success response from SAP
        return JsonResponse(
            {
                "status": True,
                "message": "Complete inspection data saved successfully!",
            },
            status=200,
        )

    except Exception as e:
        logging.exception("Rejected inspection failed")
        return JsonResponse({"status": False, "message": str(e)}, status=500)


@csrf_exempt
def save_setup_inspection(request):
    """Handle setup inspection data submission."""
    print("im inside the save setup inspection function")

    if request.method == "GET":
        return render(request, "Inspection/setup_inspection.html")

    if request.method != "POST":
        traceability_logs(
            request,
            3,
            "Invalid request method for setup inspection",
        )
        return JsonResponse({"error": "Invalid request method"}, status=405)

    try:
        plant_code = request.session.get("plant_code")
        if not plant_code:
            traceability_logs(
                request,
                3,
                "Session expired while saving setup inspection",
            )
            return JsonResponse(
                {"error": "Session expired. Please login again."},
                status=401,
            )

        db = get_db_connection(plant_code)
        collection = db["setup_inspection"]

        data = json.loads(request.body.decode("utf-8"))

        operator_id = data.get("operator_id")
        machine_id = data.get("machine_id")
        setup_count = data.get("setup_count")

        if not operator_id or not machine_id or not setup_count:
            traceability_logs(
                request,
                2,
                (
                    "Missing fields in setup inspection | "
                    f"operator:{operator_id} "
                    f"machine:{machine_id} "
                    f"setup:{setup_count}"
                ),
            )
            return JsonResponse(
                {"error": "operator_id, machine_id, setup_count are required"},
                status=400,
            )

        existing = collection.find_one(
            {
                "machine_id": machine_id,
                "inspection_type": "SETUP",
            }
        )

        if existing:
            traceability_logs(
                request,
                2,
                f"Duplicate setup inspection attempt for machine {machine_id}",
            )
            return JsonResponse(
                {"error": "Setup inspection already exists"},
                status=400,
            )

        now = timezone.now()
        shift = determine_shift(now)

        collection.insert_one(
            {
                "operator_id": operator_id,
                "machine_id": machine_id,
                "timestamp": now,
                "shift": shift,
                "setup_count": setup_count,
                "inspection_type": "SETUP",
                "status": "IN_PROGRESS",
            }
        )

        # ✅ SUCCESS LOG — correct place
        traceability_logs(
            request,
            1,
            (
                "Setup inspection saved | "
                f"Machine:{machine_id} | "
                f"Setup Count:{setup_count} | "
                f"Shift:{shift}"
            ),
        )

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse(
                {
                    "message": "Setup inspection saved successfully",
                    "shift": shift,
                    "redirect_url": "/inspection_menu/",
                }
            )

        return redirect("inspection_menu")

    except json.JSONDecodeError:
        traceability_logs(
            request,
            3,
            "Invalid JSON received in setup inspection",
        )
        return JsonResponse(
            {"error": "Invalid JSON data"},
            status=400,
        )

    except Exception as e:
        traceability_logs(
            request,
            3,
            f"Exception in setup inspection: {str(e)}",
        )
        logger.critical(
            "Setup inspection error: %s\n%s",
            str(e),
            traceback.format_exc(),
        )
        return JsonResponse(
            {"error": "An unexpected server error occurred"},
            status=500,
        )
