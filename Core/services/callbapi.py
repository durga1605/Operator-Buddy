"""
Service module to call SAP BAPI functions via pyrfc with token-based authentication.
"""

import json
import logging
from decimal import Decimal
import traceback
import requests
from pyrfc import Connection
from django.http import JsonResponse
from django.conf import settings


def call_bapi(bapi, params):
    """
    Call a BAPI function using dynamic SAP connection parameters retrieved from API.
    """
    try:
        bapi_api = settings.BAPI_API

        # Get token
        token_response = requests.post(
            bapi_api, json={"apitype": "accesstoken"}, timeout=10
        )
        token_response.raise_for_status()
        token = token_response.json().get("token")
        headers = {"Authorization": f"Bearer {token}"}

        # Get login info
        login_response = requests.post(
            bapi_api,
            headers=headers,
            json={"apitype": "login", "client": "RPS"},
            timeout=10,
        )
        login_response.raise_for_status()
        response = login_response.json()

        # Extract connection parameters
        conn_params = {
            "ashost": response["ip"],
            "sysnr": response["sysnr"],
            "client": response["client"],
            "user": response["user_name"],
            "passwd": response["password"],
        }

        # Connect and call BAPI
        conn = Connection(**conn_params)
        result = conn.call(bapi, **params)

        # Handle Decimal serialization
        def decimal_to_str(obj):
            """
            Convert Decimal objects to strings for JSON serialization.
            """
            if isinstance(obj, Decimal):
                return str(obj)
            raise TypeError("Object of type Decimal expected")

        json_data = json.dumps(result, indent=4, default=decimal_to_str)
        return {"data": json.loads(json_data), "status": True}

    except requests.RequestException as req_err:
        return {"data": f"Request failed: {req_err}", "status": False}
    except Exception as e:
        logging.critical("Unexpected error: %s\n%s", str(e), traceback.format_exc())
        return JsonResponse(
            {"error": "An unexpected server error occurred."}, status=500
        )
