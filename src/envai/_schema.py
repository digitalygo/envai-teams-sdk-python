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
LEGACY_EVENTS = load("events-v2.json")
LEGACY_OPENAPI = load("openapi-v2.json")
OPERATIONS = load("operations.json")
EVENT_VALIDATOR = Draft202012Validator(
    {"$ref": "#/$defs/Event", **EVENTS}, format_checker=FormatChecker()
)


LEGACY_EVENT_VALIDATOR = Draft202012Validator(
    {"$ref": "#/$defs/Event", **LEGACY_EVENTS}, format_checker=FormatChecker()
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


def finite_json_numbers(value: Any) -> None:
    """Keep direct dictionary event parsing as strict as wire JSON parsing."""
    if type(value) in (int, float):
        try:
            if not math.isfinite(value):
                raise ValueError("Non-finite JSON number")
        except OverflowError as exc:
            raise ValueError("JSON number exceeds finite binary64") from exc
    if isinstance(value, dict):
        for child in value.values():
            finite_json_numbers(child)
    elif isinstance(value, list):
        for child in value:
            finite_json_numbers(child)


def validate_legacy_costs(value: Any) -> None:
    """Legacy metadata must not hide nullable/negative costs in additive objects."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ("cost_usd", "cost", "chat_cost"):
                try:
                    valid = type(child) in (int, float) and child >= 0 and math.isfinite(child)
                except OverflowError:
                    valid = False
                if not valid:
                    raise ProtocolError(
                        "Legacy costs must be finite nonnegative numbers", raw=value
                    )
            validate_legacy_costs(child)
    elif isinstance(value, list):
        for child in value:
            validate_legacy_costs(child)
