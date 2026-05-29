"""Helpers for WIP scan workflow (part lookup, work-order quantity checks)."""

from typing import Any, Dict, Optional


def find_part_document(
    db,
    *,
    part_no: Optional[str] = None,
    part_name: Optional[str] = None,
    material_code: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Resolve a part from PMS_partno using part name and/or material code.
    """
    collection = db["PMS_partno"]
    name = (part_name or part_no or "").strip()
    code = (material_code or "").strip()

    if name:
        doc = collection.find_one(
            {"part_name": {"$regex": f"^{name}$", "$options": "i"}}
        )
        if doc:
            return doc

    if code:
        doc = collection.find_one({"material_code": code})
        if doc:
            return doc
        doc = collection.find_one(
            {"material_code": {"$regex": f"^{code}$", "$options": "i"}}
        )
        if doc:
            return doc

    return None


def calculate_used_work_order_quantity(trace_docs):
    """Sum quantities already posted for a work order in PCB_Trace."""
    used = 0
    for doc in trace_docs:
        status = (doc.get("status") or "").upper()
        if status not in ("COMPLETED", "OK"):
            continue
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
    """Remaining quantity allowed for a work order."""
    trace_docs = list(db["PCB_Trace"].find({"work_order": work_order}))
    used_qty = calculate_used_work_order_quantity(trace_docs)
    return max(0, int(float(total_qty or 0)) - used_qty)


def is_work_order_closed_in_trace(db, work_order, total_qty):
    """True when local trace shows no remaining quantity for the WO."""
    remaining = calculate_remaining_work_order_quantity(db, work_order, total_qty)
    return remaining <= 0


def is_sap_work_order_unavailable(process_data, woqty):
    """True when SAP returns no open stock for the work order."""
    if not process_data:
        return True
    if float(woqty or 0) <= 0:
        stock_values = [
            item.get("IP_STOCKQTY")
            for item in process_data
            if item.get("IP_STOCKQTY") not in (None, "")
        ]
        if not stock_values:
            return True
        if all(float(v or 0) <= 0 for v in stock_values):
            return True
    return False
