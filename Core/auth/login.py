"""Views for handling user sign-in, authentication,"""

from functools import wraps
import traceback
import logging

import requests

from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect, render
from django.conf import settings
from ..components.db_connection_string import get_db_connection
from Core.auth.logs import traceability_logs


def sign_out(request):
    """Logs out the user and redirects to login."""
    logout(request)
    return redirect("signin")


def unauth(request):
    """Renders the unauthorized access page."""
    return render(request, "auth/unauthorization.html")


def mongo_login_required(view_func):
    """Decorator to require login for views using MongoDB session."""

    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not hasattr(request, "session") or "user_id" not in request.session:
            return redirect(f"/?next={request.path}")
        return view_func(request, *args, **kwargs)

    return _wrapped_view


def sign_in(request):
    """Renders the sign-in authentication page and handles login logic."""

    if request.session.get("user_id"):
        return redirect(request.GET.get("next", "/Core/mobility"))

    employee_code = False

    if request.method == "POST":
        try:
            employee_code = request.POST["username"]
            password = request.POST["password"]

            login_api = settings.APILOGIN
            response = requests.post(
                login_api,
                json={"apitype": "accesstoken"},
                timeout=10,
            )

            if response.status_code != 200:
                messages.error(request, "AMS Api Failed")
                return render(request, "auth/login.html")

            token = response.json()["token"]

            auth_response = requests.post(
                login_api,
                json={
                    "apitype": "login",
                    "username": employee_code,
                    "password": password,
                    "token": token,
                },
                timeout=10,
            )

            auth_status = auth_response.json()

            if auth_status["result"]:

                user_data = {
                    "employee_name": auth_status["employeename"],
                    "gender": auth_status["gender"],
                    "doj": auth_status["employee_doj"],
                    "dob": auth_status["employee_dob"],
                    "approver_id": auth_status["approver_id"],
                    "department": {
                        "code": auth_status["departmentcode"],
                        "name": auth_status["departmentname"],
                    },
                    "designation": {
                        "code": auth_status["designationcode"],
                        "name": auth_status["designationname"],
                    },
                    "plant": {
                        "code": auth_status["locationcode"],
                        "name": auth_status["locationname"],
                    },
                    "division": {
                        "code": auth_status["division_code"],
                        "name": auth_status["division_name"],
                    },
                    "grade": {
                        "code": auth_status["grade_code"],
                        "name": auth_status["grade_name"],
                    },
                    "section": {
                        "code": auth_status["section_code"],
                        "name": auth_status["section_name"],
                    },
                    "mobile": auth_status["mobile"],
                    "email": auth_status["email_id"],
                }

                plant_code = user_data["plant"]["code"]
                request.session["plant_code"] = plant_code

                if not plant_code:
                    messages.error(
                        request,
                        "Your session has expired. Please log in again.",
                    )
                    return redirect("signin")

                try:
                    db = get_db_connection(plant_code)
                    if db is None:
                        return render(
                            request,
                            "auth/db_error.html",
                            {
                                "error_message": (
                                    "Database not connected. "
                                    "Please contact your administrator."
                                )
                            },
                        )
                except ValueError:
                    return render(
                        request,
                        "auth/unauthorization.html",
                        {
                            "error_message": (
                                f"Your plant code ({plant_code}) "
                                "is not supported in the system. "
                                "Please contact administrator."
                            )
                        },
                    )

                collection = db["trace_users"]
                employee_collection = db["trace_employees"]

                existing_user = collection.users.find_one({"username": employee_code})

                if existing_user:
                    request.session["user_id"] = str(existing_user["_id"])
                    request.session["employee_code"] = str(existing_user["username"])
                    request.session["employee_name"] = user_data["employee_name"]

                    emp = employee_collection.find_one({"employee_code": employee_code})

                    if emp and "role_id" in emp:
                        request.session["role_id"] = emp["role_id"]
                        traceability_logs(
                            request,
                            0,
                            (
                                f"Retrieved role_id {emp['role_id']} "
                                f"for {employee_code}"
                            ),
                            username=employee_code,
                            name=auth_status.get("employeename", "---"),
                        )

                    update_data = {
                        "plant": user_data["plant"],
                        "department": user_data["department"],
                        "division": user_data["division"],
                        "designation": user_data["designation"],
                        "grade": user_data["grade"],
                        "section": user_data["section"],
                        "employee_name": user_data["employee_name"],
                        "gender": user_data["gender"],
                        "doj": user_data["doj"],
                        "dob": user_data["dob"],
                    }

                    employee_collection.update_one(
                        {"employee_code": employee_code},
                        {"$set": update_data},
                    )

                else:
                    user_id = collection.users.insert_one(
                        {
                            "username": employee_code,
                            "first_name": user_data["employee_name"],
                        }
                    ).inserted_id

                    employee_collection.insert_one(
                        {
                            "employee_code": employee_code,
                            "employee_name": user_data["employee_name"],
                            "role_id": "1",
                            "plant": user_data["plant"],
                            "department": user_data["department"],
                            "division": user_data["division"],
                            "designation": user_data["designation"],
                            "grade": user_data["grade"],
                            "section": user_data["section"],
                            "gender": user_data["gender"],
                            "mobile": user_data["mobile"],
                            "email": str(user_data["email"]).lower(),
                            "dob": user_data["dob"],
                            "doj": user_data["doj"],
                        }
                    )

                    request.session["user_id"] = str(user_id)
                    request.session["employee_name"] = user_data["employee_name"]
                    request.session["role_id"] = "1"

                    traceability_logs(
                        request,
                        1,
                        f"New user {employee_code} created and logged in",
                        username=employee_code,
                        name=auth_status.get("employeename", "---"),
                    )

                role_id = str(request.session.get("role_id"))

                if role_id == "2":
                    response = redirect("plant_selection")
                else:
                    response = redirect("mobility")

                response.set_cookie(
                    "prev_qmsUser",
                    employee_code,
                    max_age=60 * 60 * 60,
                )
                return response

            if auth_status["message"] == "Access Denied":
                traceability_logs(
                    request,
                    2,
                    f"Access denied for user {employee_code}",
                )
                messages.error(
                    request,
                    "Too Many Attempts. Try Again Later",
                )
                return render(
                    request,
                    "auth/login.html",
                    {
                        "employee_code": (
                            employee_code or request.COOKIES.get("prev_qmsUser")
                        ),
                    },
                )

            messages.error(request, "Invalid username or password")

        except Exception:
            plant_code = request.session.get("plant_code")
            print(plant_code, " in except block")

            try:
                if plant_code:
                    db = get_db_connection(plant_code)
                    traceability_logs(
                        request,
                        3,
                        traceback.format_exc(),
                    )
                else:

                    logging.error(traceback.format_exc())
            except Exception:

                logging.error(traceback.format_exc())

            messages.error(
                request,
                "Database Not Connected Something Went Wrong Contact Admin",
            )

    return render(
        request,
        "auth/login.html",
        {
            "employee_code": (employee_code or request.COOKIES.get("prev_qmsUser")),
        },
    )
