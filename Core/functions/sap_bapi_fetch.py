"""Utility functions for fetching work order and process details from BAPI."""

import logging
from datetime import datetime

from Core.functions.callbapi import call_bapi

logger = logging.getLogger(__name__)


def fetch_process_details(work_order, sap_plant_code):
    """Fetch process details for a work order using BAPI."""
    try:
        bapi_params = {
            "LV_WONO": work_order,
            "PLANT": str(sap_plant_code),
            "LV_FLAG": "Q",
        }
        bapi_response = call_bapi("ZPP_FM_MATWORKCENTER_DET", bapi_params)

        print("bapi_response:", bapi_response)
        if not isinstance(bapi_response, dict):
            return None, False, "Invalid response from BAPI", 0, []

        process_data = bapi_response.get("data", {})
        if not isinstance(process_data, dict):
            return None, False, str(process_data), 0, []
        process_data = process_data.get("IT_WOSTK_DET", [])
        logger.debug(
            "Full BAPI response for ZPP_FM_MATWORKCENTER_DET: %s", bapi_response
        )

        if not process_data:
            return (
                None,
                False,
                "Work order not found or already completed in SAP.",
                0,
                [],
            )

        woqty = 0
        if process_data and isinstance(process_data, list):
            try:
                first_qty = next(
                    (
                        item.get("IP_STOCKQTY")
                        for item in process_data
                        if item.get("IP_STOCKQTY") not in (None, "")
                    ),
                    0,
                )
                woqty = float(first_qty or 0)
            except Exception:
                woqty = 0

        material_plant_list = [
            {
                "MATERIAL": item.get("OP_MATERIAL"),
                "PLANT": item.get("PLANT"),
                "OP_DESCRIPTION": item.get("OP_DESCRIPTION"),
                "WOQTY": item.get("WOQTY"),
                "IP_MATERIAL": item.get("IP_MATERIAL", ""),
                "IP_DESCRIPTION": item.get("IP_DESCRIPTION", ""),
                "IP_SLOC": item.get("IP_SLOC", ""),
                "ALT_BOM": item.get("ALT_BOM", ""),
                "IP_STOCKQTY": item.get("IP_STOCKQTY", 0),
                "LOT_QTY": item.get("LOT_QTY", 0),
            }
            for item in process_data
        ]
        # print("material_plant_list:", material_plant_list)

        process_result = [
            {
                "MATERIAL": item.get("OP_MATERIAL"),
                "WORK_CENTER": item.get("IDNRK"),
                "PLANT": item.get("PLANT"),
                "OP_DESCRIPTION": item.get("OP_DESCRIPTION", ""),
                "WOQTY": item.get("WOQTY"),
                "IP_MATERIAL": item.get("IP_MATERIAL", ""),
                "IP_DESCRIPTION": item.get("IP_DESCRIPTION", ""),
                "IP_SLOC": item.get("IP_SLOC", ""),
                "ALT_BOM": item.get("ALT_BOM", ""),
                "IP_STOCKQTY": item.get("IP_STOCKQTY", 0),
                "LOT_QTY": item.get("LOT_QTY", 0),
                "OP_OLDMATERIAL": item.get("OP_OLDMATERIAL", ""),
            }
            for item in process_data
        ]
        # print("process_result:", process_result)
        return (
            process_result,
            bapi_response.get("status", "unknown"),
            None,
            woqty,
            material_plant_list,
        )

    except ValueError:
        logger.exception("Value error during BAPI fetch.")
        return None, False, "Invalid value encountered.", 0, []

    except Exception as e:
        logger.error("Error fetching process details for WO %s: %s", work_order, e)
        return None, False, str(e), 0, []


def post_to_sap_prodent_ot(
    sap_code,
    work_order,
    machine_id,
    operator_id,
    rejected_count,
    ok_qty,
    material_record,
):
    """Post production entry to SAP using ZBAPI_PRODENT_OT BAPI."""
    try:
        current_date = datetime.now().strftime("%d.%m.%Y")
        current_time = datetime.now().strftime("%H:%M:%S")

        if not material_record:
            material_record = {}

        raw_emp = str(operator_id or "").strip()
        operator_no = raw_emp[-6:] if len(raw_emp) >= 6 else raw_emp.zfill(6)

        logger.debug(
            "SAP EMP formatting | raw='%s' | sending='%s'",
            raw_emp,
            operator_no,
        )
        print("EMPNO before call_bapi (formatted):", operator_no)

        payload = {
            "INPUT2": [
                {
                    "ENT_TYPE": "NEW DATE",
                    "STAGE": "WIP STAGE",
                    "PLANT": str(sap_code),
                    "DATE": current_date,
                    "TIME": current_time,
                    "WO_ORDER": work_order,
                    "RM_COIL_BATCH": "",
                    "INP_MAT": material_record.get("IP_MATERIAL") or "",
                    "INP_LOC": material_record.get("IP_SLOC") or "",
                    "INP_BATCH": "",
                    "INP_STK_QTY": float(material_record.get("IP_STOCKQTY", 0) or 0),
                    "OP_MAT": material_record.get("MATERIAL") or "",
                    "ALT_BOM": material_record.get("ALT_BOM") or "",
                    "TOOL_CODE": "",
                    "WORK_CENTER": machine_id.strip(),
                    "EMPNO": operator_no,
                    "PROD_QTY": str(ok_qty or 0),
                    "LOT_QTY": str(material_record.get("LOT_QTY", 0) or 0),
                    "REJ_QTY": str(rejected_count or 0),
                    "FLAG": "S",
                }
            ]
        }

        logger.debug("SAP payload for ZBAPI_PRODENT_OT: %s", payload)

        response = call_bapi("ZBAPI_PRODENT_OT", payload)
        print("SAP response:", response)

        if not response or "data" not in response:
            return {"status": False, "message": "No response from SAP"}

        sap_data = response.get("data", {})
        sap_return = sap_data.get("RETURN", {})
        sap_type = sap_return.get("TYPE")
        sap_message = sap_return.get("MESSAGE")

        if not sap_message:
            errors = sap_data.get("IT_ERROR1", [])
            if errors:
                sap_type = errors[0].get("TYPE")
                sap_message = errors[0].get("MESSAGE")

        if not sap_message:
            sap_message = "Unknown SAP response"

        return {"status": sap_type == "S", "message": sap_message}

    except Exception as e:
        logging.exception("SAP posting crashed")
        return {"status": False, "message": str(e)}
