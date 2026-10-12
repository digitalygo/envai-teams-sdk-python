"""Pinned schema validation without network reference resolution."""

from __future__ import annotations

import json
import math
from functools import lru_cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .errors import ProtocolError, ValidationError


def load(name: str) -> Any:
    return json.loads(files("envai").joinpath("_contracts", name).read_text())


OPENAPI = load("openapi.json")
EVENTS = load("events.json")
OPERATIONS = load("operations.json")
EVENT_VALIDATOR = Draft202012Validator(
    {"$ref": "#/$defs/Event", **EVENTS}, format_checker=FormatChecker()
)


@lru_cache(maxsize=None)
def validator(schema_json: str) -> Draft202012Validator:
    return Draft202012Validator(
        {"components": OPENAPI["components"], **json.loads(schema_json)},
        format_checker=FormatChecker(),
    )


def validate(schema: dict[str, Any], value: Any, *, request: bool = False) -> None:
    error = next(validator(json.dumps(schema)).iter_errors(value), None)
    if error:
        if request:
            raise ValidationError(
                "Request does not match the pinned contract",
                type="validation_error",
                details={"path": list(error.absolute_path), "validator": error.validator},
            )
        raise ProtocolError("Response does not match the pinned contract", raw=value)


def strict_json(text: str | bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON object key")
            result[key] = value
        return result

    def reject(value):
        raise ValueError("Non-finite JSON number")

    def number(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("Non-finite JSON number")
        return result

    return json.loads(text, object_pairs_hook=pairs, parse_constant=reject, parse_float=number)
