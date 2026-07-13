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
    # item may come from raw BAPI data (key: OP_MATERIAL) or
    # from process_result (key: MATERIAL, already remapped from OP_MATERIAL)
    op_material = (item.get("OP_MATERIAL") or item.get("MATERIAL") or "").strip()
    return {
        "MATERIAL": op_material,
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
                "ip_description": (item.get("IP_DESCRIPTION") or "").strip(),
                "op_description": (item.get("OP_DESCRIPTION") or "").strip(),
                "alt_bom": (item.get("ALT_BOM") or "").strip(),
                "material_record": _material_record_from_sap_item(item),
            }
        )
    return lines


def summarize_sap_work_order(
    process_data: List[Dict[str, Any]],
) -> Tuple[str, str, float, List[str], List[Dict[str, Any]]]:
    """
    Return material_code, part_no, woqty, unique process names, parsed lines.
    part_no comes from OP_OLDMATERIAL (fallback: OP_MATERIAL).
    """
    lines = parse_sap_work_order_lines(process_data)
    if not lines:
        return "", "", 0.0, [], []

    first = process_data[0]
    material_code = (first.get("OP_MATERIAL") or first.get("IP_MATERIAL") or "").strip()
    part_no = (first.get("OP_OLDMATERIAL") or material_code).strip()
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
    """Find SAP line matching machine (work center) and process description.

    The selected process name may come from PMS_oee_cell which uses IP_DESCRIPTION,
    while SAP lines store OP_DESCRIPTION as process_description. Both fields are
    checked so either source matches correctly.
    """
    machine_id = (machine_id or "").strip()
    process_description = (process_description or "").strip()
    if not machine_id or not process_description:
        return None

    def normalise(s: str) -> str:
        """Lower, collapse spaces/dashes/underscores for fuzzy compare."""
        return s.lower().replace(" ", "").replace("-", "").replace("_", "")

    proc_norm = normalise(process_description)
    mach_norm = normalise(machine_id)

    def _desc_matches_exact(line: Dict[str, Any]) -> bool:
        """True if process_description matches OP_DESCRIPTION or IP_DESCRIPTION (case-insensitive)."""
        proc_lower = process_description.lower()
        return (
            line["process_description"].lower() == proc_lower
            or line.get("ip_description", "").lower() == proc_lower
        )

    def _desc_matches_fuzzy(line: Dict[str, Any]) -> bool:
        """True if normalised process matches either description field."""
        return (
            normalise(line["process_description"]) == proc_norm
            or normalise(line.get("ip_description", "")) == proc_norm
        )

    # Pass 1: exact process (op or ip desc) + exact work_center
    for line in lines:
        if not _desc_matches_exact(line):
            continue
        if line["work_center"].strip() == machine_id:
            return line

    # Pass 2: exact process + machine_id contained in work_center
    for line in lines:
        if not _desc_matches_exact(line):
            continue
        if machine_id.lower() in line["work_center"].lower():
            return line

    # Pass 3: fuzzy process + exact normalised work_center
    for line in lines:
        if not _desc_matches_fuzzy(line):
            continue
        if normalise(line["work_center"]) == mach_norm:
            return line

    # Pass 4: fuzzy process + machine contained in work_center (normalised)
    for line in lines:
        if not _desc_matches_fuzzy(line):
            continue
        if mach_norm in normalise(line["work_center"]):
            return line

    # Pass 5: SAP returned no work_center — match on either description only.
    # This BAPI (ZPP_FM_MATWORKCENTER_DET) does not populate IDNRK per row.
    for line in lines:
        if not line["work_center"].strip():
            if _desc_matches_exact(line):
                return line

    # Pass 6: fuzzy process-only fallback (no work_center)
    for line in lines:
        if not line["work_center"].strip():
            if _desc_matches_fuzzy(line):
                return line

    # Pass 7: last resort — any line whose work_center is empty and there is only
    # one line (single-operation work order); the user already validated the
    # process via PMS_oee_cell so accept it.
    empty_wc_lines = [l for l in lines if not l["work_center"].strip()]
    if len(empty_wc_lines) == 1:
        return empty_wc_lines[0]

    import logging as _logging

    _logging.getLogger(__name__).warning(
        "match_sap_line MISS | machine=%r process=%r | SAP lines: %s",
        machine_id,
        process_description,
        [
            (l["work_center"], l["process_description"], l.get("ip_description", ""))
            for l in lines
        ],
    )
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
