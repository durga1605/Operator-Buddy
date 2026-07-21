"""WIP WebSocket consumer — one group per machine: machine_{id}."""

from __future__ import annotations

import json

from channels.generic.websocket import AsyncWebsocketConsumer

from Core.functions.mtlink_api import machine_group_name


class MachineProductionConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.machine_id = self.scope["url_route"]["kwargs"]["machine_id"]
        self.group_name = machine_group_name(self.machine_id)
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self.send(
            text_data=json.dumps(
                {
                    "event": "subscribed",
                    "machine_id": self.machine_id,
                    "group": self.group_name,
                }
            )
        )

    async def disconnect(self, close_code):
        if hasattr(self, "group_name"):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def machine_production_update(self, event):
        await self.send(text_data=json.dumps(event.get("payload") or {}))
