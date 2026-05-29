"""Helpers for WIP scan workflow (SAP parsing, duplicates, part lookup)."""

from typing import Any, Dict, List, Optional, Tuple


def find_part_document(
    db,
    *,
    part_no: Optional[str] = None,
    part_name: Optional[str] = None,
    material_code: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve a part from PMS_partno using part name and/or material code."""
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


def get_sap_plant_code(db, plant_code: str) -> str:
    """Resolve SAP plant code from session plant code."""
    if not plant_code:
        return ""
    doc = db["PMS_partno"].find_one(
        {"sap_plant_code": {"$exists": True, "$ne": ""}},
        {"sap_plant_code": 1},
    )
    if doc and doc.get("sap_plant_code"):
        return str(doc["sap_plant_code"])
    return str(plant_code)


def _material_record_from_sap_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Build material record dict for SAP posting from a BAPI line."""
    return {
        "MATERIAL": item.get("OP_MATERIAL"),
        "PLANT": item.get("PLANT"),
        "OP_DESCRIPTION": item.get("OP_DESCRIPTION", ""),
        "WOQTY": item.get("WOQTY"),
        "IP_MATERIAL": item.get("IP_MATERIAL", ""),
        "IP_DESCRIPTION": item.get("IP_DESCRIPTION", ""),
        "IP_SLOC": item.get("IP_SLOC", ""),
        "ALT_BOM": item.get("ALT_BOM", ""),
        "IP_STOCKQTY": item.get("IP_STOCKQTY", 0),
        "LOT_QTY": item.get("LOT_QTY", 0),
    }


def parse_sap_work_order_lines(
    process_data: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Normalize SAP IT_WOSTK_DET rows for WIP UI and validation."""
    lines = []
    for item in process_data:
        desc = (item.get("OP_DESCRIPTION") or "").strip()
        work_center = (item.get("IDNRK") or item.get("WORK_CENTER") or "").strip()
        stock_qty = float(item.get("IP_STOCKQTY") or 0)
        lines.append(
            {
                "process_description": desc,
                "work_center": work_center,
                "stock_qty": stock_qty,
                "completed_in_sap": stock_qty <= 0,
                "op_material": (item.get("OP_MATERIAL") or "").strip(),
                "ip_material": (item.get("IP_MATERIAL") or "").strip(),
                "material_record": _material_record_from_sap_item(item),
            }
        )
    return lines


def summarize_sap_work_order(
    process_data: List[Dict[str, Any]],
) -> Tuple[str, str, float, List[str], List[Dict[str, Any]]]:
    """
    Return material_code, part_no, woqty, unique process names, parsed lines.
    """
    lines = parse_sap_work_order_lines(process_data)
    if not lines:
        return "", "", 0.0, [], []

    first = process_data[0]
    material_code = (first.get("OP_MATERIAL") or first.get("IP_MATERIAL") or "").strip()
    part_no = material_code
    stock_values = [line["stock_qty"] for line in lines if line["stock_qty"] > 0]
    woqty = max(stock_values) if stock_values else float(first.get("WOQTY") or 0)

    process_names = []
    seen = set()
    for line in lines:
        name = line["process_description"]
        if name and name not in seen:
            seen.add(name)
            process_names.append(name)

    return material_code, part_no, woqty, process_names, lines


def match_sap_line(
    lines: List[Dict[str, Any]],
    machine_id: str,
    process_description: str,
) -> Optional[Dict[str, Any]]:
    """Find SAP line matching machine (work center) and process description."""
    machine_id = (machine_id or "").strip()
    process_description = (process_description or "").strip()
    if not machine_id or not process_description:
        return None

    for line in lines:
        if line["process_description"].lower() != process_description.lower():
            continue
        if line["work_center"].strip() == machine_id:
            return line

    for line in lines:
        if line["process_description"].lower() != process_description.lower():
            continue
        if machine_id.lower() in line["work_center"].lower():
            return line

    return None


def has_pcb_trace_completion(
    db,
    work_order: str,
    machine_id: str,
    process_name: str,
) -> bool:
    """True if PCB_Trace already has a completed record for WO+machine+process."""
    query = {
        "work_order": work_order,
        "machine_id": machine_id,
        "status": {"$in": ["COMPLETED", "OK"]},
    }
    if process_name:
        query["$or"] = [
            {"process_name": process_name},
            {"process_selected": process_name},
        ]
    return db["PCB_Trace"].find_one(query) is not None
