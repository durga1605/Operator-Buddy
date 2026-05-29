"""
Home view for plant selection and database connection."""

from django.shortcuts import redirect, render
from Core.components.db_connection_string import get_db_connection


def plant_selection(request):
    """Render the plant selection screen and handle database connection."""
    plant_code = request.GET.get("plant_code")

    if plant_code:
        request.session["plant_code"] = plant_code
        try:
            get_db_connection(plant_code)
        except Exception as e:
            return render(
                request,
                "plant_select/plant_select.html",
                {
                    "selected_plant": plant_code,
                    "error": f"Error connecting to DB: {str(e)}",
                    "data": [],
                },
            )

        return redirect("mobility")

    return render(request, "plant_select/plant_select.html")
