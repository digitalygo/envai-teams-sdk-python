"""Request correlation and byte bounds for native job and choice operations."""
from __future__ import annotations

import json
import unicodedata
from typing import Any

from .errors import CompatibilityError, ProtocolError


def validate_native_request(operation: str, body: dict[str, Any]) -> None:
    if operation != "classifyChoice":
        return
    if body["context"] is None:
        raise ValueError("Choice context must be non-null JSON")
    for value, limit in [(body["request_id"], 128), (body["question"], 8192)]:
        if not value.strip() or len(value.encode()) > limit:
            raise ValueError("Choice request exceeds nonempty UTF-8 bounds")
    labels: set[str] = set()
    for choice in body["choices"]:
        label, description = choice["label"], choice["description"]
        if (label != label.strip() or len(label.encode()) > 128 or label in labels
                or any(unicodedata.category(char) == "Cc" for char in label)
                or not description.strip() or len(description.encode()) > 4096):
            raise ValueError("Choice labels must be unique and descriptions bounded")
        labels.add(label)
    if len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()) > 1 << 20:
        raise ValueError("Choice request exceeds one MiB")


def validate_native_response(op: dict[str, Any], value: dict[str, Any]) -> None:
    operation = op.get("operation")
    if operation == "startIndependentJob" and (not value["task_id"] or not value["turn_id"]):
        raise ProtocolError("Independent job receipt requires nonempty task and turn IDs")
    if operation == "classifyChoice":
        body = op["body"]
        if (value["request_id"] != body["request_id"] or not value["model"]
                or not value["invocation_id"]
                or value["choice"] not in {item["label"] for item in body["choices"]}):
            raise ProtocolError("Choice result does not match the requested classification")
    if operation == "getDetachedTask":
        if value["task_id"] != op["path_params"]["task_id"]:
            raise ProtocolError("Task lookup returned a different task")
        if value["origin_turn_id"] == "" and not op.get("independent_jobs"):
            raise CompatibilityError("Originless task requires independent_jobs capability")
