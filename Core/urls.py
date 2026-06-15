"""
URL configuration for Core.
"""

from django.conf.urls.static import static
from django.urls import path

from Config import settings
from Core.auth import login
from Core.views import home, select_plant
from Core.views.wip import start_process

urlpatterns = [
    path("", login.sign_in, name="signin"),
    path("unauth", login.unauth, name="unauth"),
    path("signout", login.sign_out, name="signout"),
    path("mobility/", home.home_page, name="mobility"),
    path("plant_selection/", select_plant.plant_selection, name="plant_selection"),
    path("wip/", start_process.wip_scan_page, name="wip_scan_page"),
    path("wip/scan-operator/", start_process.scan_operator, name="scan_operator"),
    path("wip/scan-work-order/", start_process.scan_work_order, name="scan_work_order"),
    path(
        "wip/validate-machine-process/",
        start_process.validate_machine_process,
        name="validate_machine_process",
    ),
    path(
        "wip/get-machine-processes/",
        start_process.get_machine_processes,
        name="get_machine_processes",
    ),
    path("wip/scan-machine/", start_process.scan_machine, name="scan_machine"),
    path("wip/submit/", start_process.submit_production, name="submit_production"),
]
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
