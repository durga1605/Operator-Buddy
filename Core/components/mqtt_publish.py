"""MQTT publishing utility for blanking actions in the Mobility app."""

from datetime import datetime
import json
import uuid
import paho.mqtt.client as mqtt

BROKER = "localhost"  # change if broker IP
PORT = 1883


def publish_mqtt(plant_code, machine_number, action_status, payload):
    """Publish MQTT message to the broker with the given parameters."""
    topic = f"LGB/{plant_code}/{machine_number}/{action_status}/actions"

    client = mqtt.Client()

    client.connect(BROKER, PORT, 60)

    message = {
        "msg_id": str(uuid.uuid4()),
        "timestamp": datetime.now().isoformat(),
        "data": payload,
    }

    client.publish(topic, json.dumps(message))
    client.disconnect()

    return {"status": "success", "topic": topic, "msg_id": message["msg_id"]}
