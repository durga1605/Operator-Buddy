"""Logging utilities for the Core application of the Traceability project."""

from datetime import datetime
from django.shortcuts import render
from ..services.db_connection_string import get_db_connection


def traceability_logs(request, level, message, username=None, name=None):
    """
    Logs an event to the PMS_logs collection.
    level: 0=info, 1=success, 2=warning, 3=error
    message: Log message
    username: Optional username
    name: Optional employee name
    """
    plant_code = ""
    if not plant_code:
        print(plant_code)
        plant_code = request.session.get("plant_code")

    if not plant_code:
        raise ValueError("Plant code is required for logging traceability")
    db = get_db_connection(plant_code)
    if db is None:
        return render(
            request,
            "dberror.html",
            {
                "error_message": (
                    "Database not connected. Please contact your administrator."
                )
            },
        )
    logs_collection = db["traceability_log"]
    log_entry = {
        "timestamp": datetime.now(),
        "level": level,
        "message": message,
        "url_path": request.path,
        "ip": get_client_ip(request),
        "username": username or request.session.get("employee_code", ""),
        "name": name or request.session.get("employee_name", ""),
    }
    logs_collection.insert_one(log_entry)
    return log_entry


def get_client_ip(request):
    """
    Returns the client IP address from the request.
    """
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        ip = x_forwarded_for.split(",")[0]
    else:
        ip = request.META.get("REMOTE_ADDR")
    return ip
