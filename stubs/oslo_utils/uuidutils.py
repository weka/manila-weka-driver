"""Minimal oslo_utils.uuidutils stub for unit tests."""
import uuid


def generate_uuid(dashed=True):
    val = uuid.uuid4()
    return str(val) if dashed else val.hex
