"""Utility functions for inspection operations in the Mobility module."""

import re
from datetime import datetime


def check_existing_entry(
    collection, work_order, machine_id, machine_description, shift, date
):
    """Check if an entry exists for the given parameters on the specified date."""
    return (
        collection.find_one(
            {
                "work_order": work_order,
                "machine_id": machine_id,
                "machine_description": machine_description,
                "shift": shift,
                "timestamp": {
                    "$gte": datetime.combine(date, datetime.min.time()),
                    "$lte": datetime.combine(date, datetime.max.time()),
                },
            }
        )
        is not None
    )


def validate_rejection_check(collection, work_order, machine_description):
    """
    Validate if rejection can proceed for the given work order and machine description.
    """

    # Fetch the process flow (assuming saved in DB)
    process_record = collection.find_one({"work_order": work_order})
    if not process_record:
        return True  # No process started yet, allow rejection

    process_sequence = process_record.get("process_sequence", [])
    completed_processes = process_record.get("completed_processes", [])

    # If process sequence is not defined, allow
    if not process_sequence:
        return True

    # If all steps are already completed
    if len(completed_processes) >= len(process_sequence):
        return False  # Cannot reject — already completed all

    # Determine the expected next process
    expected_next_process = process_sequence[len(completed_processes)]

    # Compare with current machine_description
    if expected_next_process != machine_description:
        return False

    return True


def calculate_work_order_count(collection, work_order, input_count):
    """Calculate the work order count and flag for the given work order."""
    last_entry = collection.find_one(
        {"work_order": work_order}, sort=[("timestamp", -1)]
    )
    if last_entry:
        last_count = last_entry.get("work_order_count", 0)
        last_rejected = last_entry.get("rejected_count", 0)
        return last_count - last_rejected, 1
    return input_count, 0


def get_total_work_orders(collection, machine_id):
    """Get the total number of work orders for a given machine."""
    result = list(
        collection.aggregate(
            [
                {"$match": {"machine_id": machine_id}},
                {"$group": {"_id": "$machine_id", "total_work_orders": {"$sum": 1}}},
            ]
        )
    )
    return result[0]["total_work_orders"] if result else 0


def validate_process_sequence(
    db,
    work_order,
    scanned_machine_id,
    scanned_machine_description,
    scanned_part_name=None,
):
    """Validate the process sequence for a scanned machine and work order."""
    scanned_machine_id = scanned_machine_id.strip()
    scanned_machine_description = scanned_machine_description.strip()
    # Fetch trace entries (ordered) for this work order and part
    trace_filter = {"work_order": work_order}
    if scanned_part_name:
        trace_filter["part_name"] = {
            "$regex": f"^{re.escape(scanned_part_name)}$",
            "$options": "i",
        }

    trace_entries = list(db["PCB_Trace"].find(trace_filter).sort("timestamp", 1))

    print("Existing Trace Entries:", len(trace_entries))

    # Fetch process configuration
    config_filter = {"processes.process.machines": {"$exists": True}}
    if scanned_part_name:
        config_filter["part_name"] = {
            "$regex": f"^{re.escape(scanned_part_name)}$",
            "$options": "i",
        }

    process_config = db["PMS_oee_cell"].find_one(config_filter)

    if not process_config:
        return False, "No process configuration found."

    try:
        process_list = process_config["processes"][0]["process"]
    except (KeyError, IndexError, TypeError):
        return False, "Invalid process configuration structure."

    # Build unchecked process steps
    unchecked_processes = []
    for i, proc in enumerate(process_list):
        if proc.get("isChecked", 0) == 1:
            continue

        unchecked_processes.append(
            {
                "index": i,
                "description": proc.get("description", "").strip(),
                "machines": [
                    m["machineName"].strip() for m in proc.get("machines", [])
                ],
            }
        )

    if not unchecked_processes:
        return False, "No unchecked process steps found."

    print("Configured Process Steps (Unchecked Only):")
    for step in unchecked_processes:
        print(step["index"], step["description"], step["machines"])

    # ==========================
    # CASE 1: First scan
    # ==========================
    if not trace_entries:
        first_step = unchecked_processes[0]

        if scanned_machine_id in first_step["machines"]:
            return True, ""

        return (
            False,
            f"New work order must start with '{first_step['description']}'. "
            f"Expected machine IDs: {', '.join(first_step['machines'])}. "
            f"You scanned: '{scanned_machine_id}'",
        )

    # ==========================
    # CASE 2: Continuing work order
    # ==========================
    last_entry = trace_entries[-1]
    last_machine_desc = last_entry.get("machine_description", "").strip()

    # Block duplicate completion
    for entry in trace_entries:
        if scanned_machine_description == entry.get("machine_description", "").strip():
            return (
                False,
                f"Process already completed for machine: {scanned_machine_description}. "
                "Proceed to the next process.",
            )

    # 🔴 FIX: Find last completed process using DESCRIPTION (NOT machine_id)
    last_index = -1
    for i, proc in enumerate(process_list):
        proc_desc = proc.get("description", "").strip()
        if proc_desc == last_machine_desc:
            last_index = i
            break

    if last_index == -1:
        return (
            False,
            f"Last completed process '{last_machine_desc}' not found in process configuration.",
        )

    # Find next unchecked process step
    next_step = None
    for step in unchecked_processes:
        if step["index"] > last_index:
            next_step = step
            break

    if not next_step:
        return False, "All unchecked processes already completed."

    # Validate scanned machine
    if scanned_machine_id in next_step["machines"]:
        print(">>> Process sequence validated successfully")
        return True, ""
    previous_process = last_machine_desc
    current_process = next_step["description"]

    message = (
        f"Expected next process: '{current_process}'. "
        f"Expected machine IDs: {', '.join(next_step['machines'])}. "
        f"You scanned: '{scanned_machine_id}'. "
        f"{previous_process} is not completed. "
        f"Please complete it before proceeding to {current_process}."
    )

    return False, message
