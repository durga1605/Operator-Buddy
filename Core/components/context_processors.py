"""
Context processor for injecting the employee name into Django templates.
"""


def custom_context(request):
    """
    Example custom context processor.
    """
    return {}


def another_context(request):
    """
    Another example context processor.
    """
    return {}


def employee_name(request):
    """
    Return a dictionary with the employee name from the session for use in templates.
    """
    return {
        "employee_name": request.session.get("employee_name", ""),
        "plant_code": request.session.get("plant_code", ""),
    }


def user_role(request):
    """
    Adds the user's role_id from the session to the template context.
    """
    return {"role_id": request.session.get("role_id")}
