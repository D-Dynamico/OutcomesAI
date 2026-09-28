import uuid

# Synthetic, in the style of the brief's TelephoneTriage example
TRANSCRIPT = ("Nurse: Triage line, what's going on today? Patient: I've had a fever since last night "
              "and now my throat hurts. Nurse: Any difficulty breathing or swallowing?")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def make_event(*, transcription: str = TRANSCRIPT, **overrides) -> dict:
    event = {
        "event_id": new_id("evt"),
        "encounter_id": new_id("enc"),
        "patient_id": "pat-77",
        "encounter_type": "TelephoneTriage",
        "version": 12,
        "payload": {"transcription": transcription},
    }
    event.update(overrides)
    return event


def post(client, event: dict):
    return client.post("/encounters/events", json=event)
