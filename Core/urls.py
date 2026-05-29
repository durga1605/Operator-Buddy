"""
URL configuration for Core.
"""

from django.urls import path
from Core.views import select_plant, home
from Core.views.wip import start_process
from Config import settings
from django.conf.urls.static import static
from Core.auth import login

urlpatterns = [
    path("", login.sign_in, name="signin"),
    path("unauth", login.unauth, name="unauth"),
    path("signout", login.sign_out, name="signout"),
    path("mobility/", home.home_page, name="mobility"),
    path("plant_selection/", select_plant.plant_selection, name="plant_selection"),
    path("wip/", start_process.wip_scan_page, name="wip_scan_page"),
    path("wip/scan-part/", start_process.scan_part_no, name="scan_part_no"),
    path("wip/scan-operator/", start_process.scan_operator, name="scan_operator"),
    path("wip/scan-machine/", start_process.scan_machine, name="scan_machine"),
    path("wip/scan-work-order/", start_process.scan_work_order, name="scan_work_order"),
    path("wip/submit/", start_process.submit_production, name="submit_production"),
]
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
