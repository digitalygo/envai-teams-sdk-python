"""Deterministically generate types and REST primitives from the pinned contract.

Runtime ATP and tool safety logic is deliberately handwritten. No network access.
"""

from __future__ import annotations

import argparse
import json
import keyword
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "src/envai"
API = yaml.safe_load((DEST / "_contracts/openapi.yaml").read_text())
EVENTS = json.loads((DEST / "_contracts/events.json").read_text())
SCHEMAS = API["components"]["schemas"]
NAMES = {key: key for key in SCHEMAS}
NAMES.update(
    {
        "event:" + key: "ATP" + "".join(x.title() for x in key.split("."))
        for key in EVENTS["$defs"]
        if key not in ("Event", "Envelope")
    }
)
MODELS = {}
ALIASES = {}


def pascal(s):
    return "".join(x[:1].upper() + x[1:] for x in re.split(r"[^a-zA-Z0-9]", s))


def snake(s):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()


def field(s):
    result = re.sub(r"[^a-zA-Z0-9_]", "_", s).strip("_")
    if "-" in s:
        result = result.lower()
    return result + "_" if keyword.iskeyword(result) else result


def resolve(schema):
    if "$ref" in schema:
        ref = schema["$ref"]
        return (EVENTS["$defs"] if "/$defs/" in ref else SCHEMAS)[ref.rsplit("/", 1)[1]]
    return schema


def merge(schema):
    if "allOf" not in schema:
        return schema
    merged = {
        **schema,
        "type": "object",
        "properties": dict(schema.get("properties", {})),
        "required": list(schema.get("required", [])),
    }
    merged.pop("allOf", None)
    for part in schema["allOf"]:
        part = merge(resolve(part))
        merged["properties"].update(part.get("properties", {}))
        merged["required"] += part.get("required", [])
    return merged


def annotation(schema, name):
    if not schema or schema is True:
        return "Any"
    if "$ref" in schema:
        key = schema["$ref"].rsplit("/", 1)[1]
        return NAMES[("event:" if "/$defs/" in schema["$ref"] else "") + key]
    if "const" in schema:
        return f"Literal[{schema['const']!r}]"
    if name in ("CapabilitiesFeaturesItem", "CapabilitiesFamiliesItem"):
        return "str"
    if "enum" in schema:
        return "Literal[" + ", ".join(repr(x) for x in schema["enum"]) + "]"
    for union in ("anyOf", "oneOf"):
        if union in schema and not (schema.get("properties") and all(
            set(branch) <= {"required"} for branch in schema[union]
        )):
            return (
                "Union["
                + ", ".join(annotation(x, name + str(i)) for i, x in enumerate(schema[union]))
                + "]"
            )
    schema = merge(schema)
    typ = schema.get("type")
    if isinstance(typ, list):
        return (
            "Union["
            + ", ".join(
                annotation({**schema, "type": t}, name + "Value" if t == "object" else name)
                for t in typ
            )
            + "]"
        )
    if typ == "object" or (typ is None and "properties" in schema):
        if schema.get("properties"):
            if name not in MODELS:
                MODELS[name] = ""
                lines = [f"class {name}(Model):"]
                for key, val in schema["properties"].items():
                    ann = annotation(val, name + pascal(key))
                    optional = key not in schema.get("required", [])
                    args = ["default=None"] if optional else []
                    if optional:
                        ann = f"Union[{ann}, None]"
                    if field(key) != key:
                        args.append("alias=" + repr(key))
                    if key in ("token", "signature", "output", "args", "content", "prompt"):
                        args.append("repr=False")
                    default = (" = Field(" + ", ".join(args) + ")") if args else ""
                    lines.append(f"    {field(key)}: {ann}{default}")
                if name == "Capabilities":
                    lines.extend(
                        [
                            "",
                            '    @field_validator("features", mode="before")',
                            "    @classmethod",
                            "    def _feature_names(cls, value):",
                            "        if isinstance(value, list):",
                            "            if not all(isinstance(item, str) for item in value):",
                            '                raise ValueError("Capabilities features list must contain strings")',
                            "            return value",
                            "        if isinstance(value, dict):",
                            "            if not all(",
                            "                isinstance(feature, str) and isinstance(enabled, bool)",
                            "                for feature, enabled in value.items()",
                            "            ):",
                            '                raise ValueError("Capabilities feature map must contain boolean values")',
                            "            return [feature for feature, enabled in value.items() if enabled]",
                            '        raise ValueError("Capabilities features must be a list or a boolean map")',
                        ]
                    )
                MODELS[name] = "\n".join(lines)
            return name
        add = schema.get("additionalProperties", {})
        return (
            "dict[str, "
            + (annotation(add, name + "Value") if isinstance(add, dict) else "Any")
            + "]"
        )
    if typ == "array":
        return "list[" + annotation(schema.get("items", {}), name + "Item") + "]"
    return {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "null": "None",
    }.get(typ, "Any")


for key, schema in SCHEMAS.items():
    ann = annotation(schema, key)
    if ann != key:
        ALIASES[key] = ann
for key, schema in EVENTS["$defs"].items():
    if key in ("Event", "Envelope"):
        continue
    name = NAMES["event:" + key]
    ann = annotation(schema, name)
    if ann != name:
        ALIASES[name] = ann

header = '''# Generated by scripts/generate.py; do not edit by hand.
from __future__ import annotations
from typing import Any, Literal, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator

class Model(BaseModel):
    """Forward-compatible wire model; omission is preserved with exclude_unset."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)
'''
models = header + "\n\n" + "\n\n\n".join(MODELS.values()) + "\n\n"
models += "\n".join(k + " = " + v for k, v in ALIASES.items()) + "\n\n"
models += "\n".join(k + ".model_rebuild()" for k in MODELS) + "\n"

ops = {}
for path, entry in API["paths"].items():
    for method, op in entry.items():
        if method not in ("get", "post", "put", "delete", "patch"):
            continue
        params = []
        for p in op.get("parameters", []):
            if "$ref" in p:
                p = API["components"]["parameters"][p["$ref"].rsplit("/", 1)[1]]
            if p["name"] not in ("X-Envai-Contract-Version", "X-Envai-Extensions"):
                params.append(p)
        request = op.get("requestBody", {})
        request_content = request.get("content", {})
        media = next(iter(request_content), None)
        req = request_content.get(media, {}).get("schema")
        responses = op.get("responses", {})
        success = next((v for k, v in responses.items() if str(k).startswith("2")), None)
        content = (success or {}).get("content", {})
        res_media = next(iter(content), None)
        res = content.get(res_media, {}).get("schema")
        unwrapped = res
        unwrap = False
        if res:
            resolved = resolve(res)
            if set(resolved.get("properties", {})) == {"data"} and "data" in resolved.get(
                "required", []
            ):
                unwrapped = resolved["properties"]["data"]
                unwrap = True
        response_type = (
            "None"
            if not res
            else "bytes"
            if res_media != "application/json"
            else annotation(unwrapped, pascal(op["operationId"]) + "Response")
        )
        ops[op["operationId"]] = dict(
            method=method.upper(),
            path=path,
            params=params,
            request=req,
            media=media,
            request_required=request.get("required", False),
            response=res,
            response_type=response_type,
            unwrap=unwrap,
            auth=op.get("security", API.get("security", [])) != [],
            stream=res_media == "text/event-stream",
            public=path not in ("/tests/images/generate", "/webhooks/providers/{provider}"),
        )

# Inline endpoint response models may have been created above.
models = header + "\n\n" + "\n\n\n".join(MODELS.values()) + "\n\n"
models += "\n".join(k + " = " + v for k, v in ALIASES.items()) + "\n\n"
models += "\n".join(k + ".model_rebuild()" for k in MODELS) + "\n"

GROUPS = {
    "attachments": {
        "upload": "uploadChatAttachment",
        "list": "listChatAttachments",
        "get": "getChatAttachment",
        "delete": "deleteChatAttachment",
        "retry": "retryChatAttachment",
        "download": "downloadChatAttachment",
    },
    "workspaces": {
        "create": "createWorkspace",
        "list": "listWorkspaces",
        "get": "getWorkspace",
        "update": "updateWorkspace",
    },
    "tools": {
        "create": "createUserTool",
        "list": "listUserTools",
        "get": "getUserTool",
        "update": "updateUserTool",
        "delete": "deleteUserTool",
    },
    "chats": {
        "create": "createChat",
        "list": "listChats",
        "get": "getChat",
        "send_message": "sendChatMessage",
        "cancel": "cancelChatTurn",
        "steer": "steerChatTurn",
        "submit_tool_output": "resolveChatTool",
        "submit_approval": "resolveChatApproval",
        "fork": "forkChat",
        "restore": "restoreChat",
        "deliver": "deliverWorkflowResult",
        "delegate": "delegateToTeam",
        "get_task": "getDetachedTask",
        "cancel_task": "cancelDetachedTask",
        "classify_phase": "classifyPhase",
    },
    "completions": {"list": "listCompletions", "get": "getCompletion"},
    "wiki": {
        "upload": "uploadWikiSource",
        "source": "getWikiSource",
        "download": "downloadWikiSource",
        "withdraw": "withdrawWikiSource",
        "inbox": "createWikiInboxRun",
        "outbox": "createWikiOutboxRun",
        "sync": "createWikiSyncRun",
        "operation": "getWikiOperation",
        "categories": "listWikiCategories",
        "category_index": "getWikiCategoryIndex",
        "jwks": "getWikiWebhookJwks",
    },
    "files": {
        "get": "indexWorkspaceFiles",
        "create": "createWorkspaceFile",
        "update": "updateWorkspaceFile",
        "delete": "deleteWorkspaceFile",
        "download": "downloadWorkspaceFile",
        "convert_to_pdf": "convertWorkspaceFileToPdf",
    },
    "agents": {
        "create": "createAgent",
        "list": "listAgents",
        "get": "getAgent",
        "update": "updateAgent",
        "delete": "deleteAgent",
    },
    "teams": {
        "create": "createTeam",
        "list": "listTeams",
        "get": "getTeam",
        "update": "updateTeam",
        "delete": "deleteTeam",
    },
    "api_keys": {"create": "createApiKey", "list": "listApiKeys", "delete": "deleteApiKey"},
    "images": {
        "generate": "createImageGeneration",
        "get": "getImageGeneration",
        "upload": "uploadImage",
        "models": "listImageModels",
        "download_artifact": "downloadArtifact",
    },
    "models": {"list": "listModels"},
    "capabilities": {"get": "getCapabilities"},
}


def method_code(operation, alias, async_, target):
    op = ops[operation]
    params = ["self"]
    args = []
    query, headers = [], []
    for p in op["params"]:
        if p["in"] == "path":
            params.append(field(p["name"]) + ": str")
            args.append(repr(p["name"]) + ": " + field(p["name"]))
    params.append("*")
    if op["request"]:
        if op["media"] == "multipart/form-data":
            params += [
                "files: dict[str, Any] | list[tuple[str, Any]]",
                "data: dict[str, Any] | None = None",
            ]
        else:
            ann = annotation(op["request"], pascal(operation) + "Body")
            params.append(
                "body: models."
                + ann
                + " | dict[str, Any]"
                + ("" if op["request_required"] else " | None = None")
            )
    for p in op["params"]:
        if p["in"] != "path":
            name = field(p["name"])
            ann = annotation(p.get("schema", {}), pascal(operation) + pascal(name))
            params.append(name + ": " + ann + ("" if p.get("required") else " | None = None"))
            (query if p["in"] == "query" else headers).append(repr(p["name"]) + ": " + name)
    params.append("request_options: RequestOptions | None = None")
    typ = op["response_type"]
    # Qualify generated names without touching builtin types.
    typ = re.sub(
        r"\b[A-Z][A-Za-z0-9]*\b",
        lambda m: m[0] if m[0] in ("None", "Any", "Literal", "Union") else "models." + m[0],
        typ,
    )
    prefix = "async def" if async_ else "def"
    code = f"    {prefix} {alias}({', '.join(params)}) -> {typ}:\n"
    code += f'        """{operation}: {op["method"]} {op["path"]}."""\n'
    call = f"{target}._request({operation!r}, path_params={{{', '.join(args)}}}, query={{{', '.join(query)}}}, headers={{{', '.join(headers)}}}, request_options=request_options"
    if op["request"]:
        call += (
            ", files=files, data=data" if op["media"] == "multipart/form-data" else ", body=body"
        )
    code += "        return " + ("await " if async_ else "") + call + ")\n"
    return code


runtime = """# Generated by scripts/generate.py; do not edit by hand.
from __future__ import annotations
from typing import Any, Literal, Union
from . import models
from ._transport import RequestOptions
"""
runtime += "\nRESPONSE_TYPES = {\n"
for typ in sorted({op["response_type"] for op in ops.values()}):
    qualified = re.sub(
        r"\b[A-Z][A-Za-z0-9]*\b",
        lambda m: m[0] if m[0] in ("None", "Any", "Literal", "Union") else "models." + m[0],
        typ,
    )
    runtime += f"    {typ!r}: {qualified},\n"
runtime += "}\n"

for async_ in (False, True):
    prefix = "Async" if async_ else ""
    runtime += f"\nclass {prefix}Operations:\n"
    runtime += "    _request: Any\n"
    for operation, op in ops.items():
        if not op["stream"]:
            runtime += method_code(operation, snake(operation), async_, "self") + "\n"
    for group, aliases in GROUPS.items():
        runtime += f"\nclass {prefix}{pascal(group)}Service:\n    def __init__(self, client: Any):\n        self._client = client\n\n"
        for alias, operation in aliases.items():
            runtime += method_code(operation, alias, async_, "self._client") + "\n"

event_models = """# Generated by scripts/generate.py; do not edit by hand.
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal, Union
from . import models
from .events import Event

"""
event_names = {}
for key in EVENTS["$defs"]:
    if key != "ping" and "." not in key:
        continue
    name = "".join(x.title() for x in key.split(".")) + "Event"
    event_names[key] = name
    event_models += f"@dataclass(frozen=True)\nclass {name}(Event):\n    type: Literal[{key!r}]\n    data: models.{NAMES['event:' + key]}\n\n"
event_models += "KnownEvent = Union[" + ", ".join(event_names.values()) + "]\n"
event_models += (
    "EVENT_TYPES = {\n"
    + "".join(f"    {key!r}: {name},\n" for key, name in event_names.items())
    + "}\n"
)

# Request-inline models are discovered while generating methods above.
models = header + "\n\n" + "\n\n\n".join(MODELS.values()) + "\n\n"
models += "\n".join(k + " = " + v for k, v in ALIASES.items()) + "\n\n"
models += "\n".join(k + ".model_rebuild()" for k in MODELS) + "\n"

outputs = {
    "event_models.py": event_models,
    "models.py": models,
    "_generated.py": runtime,
    "_contracts/operations.json": json.dumps(ops, indent=2) + "\n",
    "_contracts/openapi.json": json.dumps(API, indent=2) + "\n",
}
parser = argparse.ArgumentParser()
parser.add_argument("--check", action="store_true")
a = parser.parse_args()
for path, text in outputs.items():
    p = DEST / path
    if a.check:
        if p.read_text() != text:
            raise SystemExit("generated drift: " + str(p))
    else:
        p.write_text(text)
print(f"{len(ops)} operations; {len(MODELS)} models; {len(EVENTS['$defs']) - 5} event payloads")
