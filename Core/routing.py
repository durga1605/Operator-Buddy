"""WebSocket URL routes."""

from django.urls import re_path

from Core.consumers import MachineProductionConsumer

websocket_urlpatterns = [
    re_path(
        r"^ws/machine/(?P<machine_id>[^/]+)/$",
        MachineProductionConsumer.as_asgi(),
    ),
]
