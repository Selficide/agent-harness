"""Contract v1 schemas and a small validator for the JSON Schema subset they use.

The subset (see docs/plan-contract-v1.md, section 1): type (a name or a list of names), properties, required,
additionalProperties (false or true only), items, enum, const, minLength/maxLength, minItems, minimum/maximum, plus
the annotations description, $schema, $id and title. Anything else is rejected by check_schema, so a schema can't
silently rely on a keyword the validator ignores.
"""

import json
import ntpath
import posixpath
import re
from pathlib import Path

CONTRACT_DIR = Path(__file__).resolve().parent.parent / "contract" / "v1"
CONTRACT_VERSION = "1"
SCHEMA_FILES = ("request.schema.json", "result.schema.json", "answer.review.json", "answer.research.json")

TYPE_NAMES = {"object", "array", "string", "integer", "number", "boolean", "null"}
ANNOTATIONS = {"description", "$schema", "$id", "title"}
KEYWORDS = {"type", "properties", "required", "additionalProperties", "items", "enum", "const", "minLength",
            "maxLength", "minItems", "minimum", "maximum"} | ANNOTATIONS
# Claude Code's rule for MCP tool input property names.
PROPERTY_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

_cache = {}


def load(name):
    """A contract schema by file name, parsed once."""
    if name not in _cache:
        _cache[name] = json.loads((CONTRACT_DIR / name).read_text(encoding="utf-8"))
    return _cache[name]


def answer_schema(task_type):
    return load(f"answer.{task_type}.json")


def strip_annotations(schema):
    """The schema without $schema/$id/title, as handed to a provider's structured-output option."""
    if isinstance(schema, dict):
        return {k: strip_annotations(v) for k, v in schema.items() if k not in ("$schema", "$id", "title")
                or not isinstance(v, str)}
    if isinstance(schema, list):
        return [strip_annotations(v) for v in schema]
    return schema


# --- schema checks -----------------------------------------------------------------------------------------------

def check_schema(schema, strict=False, mcp_names=False, path="#"):
    """Problems with a schema under the subset rules, as a list of strings (empty when fine).

    strict: Codex structured-output rules: every object lists all its properties in required and sets
    additionalProperties to false. mcp_names: property names must suit MCP tool input schemas.
    """
    problems = []
    if not isinstance(schema, dict):
        return [f"{path}: schema must be an object"]
    for key in schema:
        if key not in KEYWORDS:
            problems.append(f"{path}: keyword {key!r} is outside the supported subset")
    types = _types(schema)
    if types is None:
        problems.append(f"{path}: 'type' is required and must be a type name or a list of them")
        types = set()
    elif not types <= TYPE_NAMES:
        problems.append(f"{path}: unknown type(s) {sorted(types - TYPE_NAMES)}")
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        problems.append(f"{path}: additionalProperties must be true or false")
    if "object" in types:
        props = schema.get("properties", {})
        required = schema.get("required", [])
        for name in required:
            if name not in props:
                problems.append(f"{path}: required {name!r} is not in properties")
        if strict:
            if schema.get("additionalProperties") is not False:
                problems.append(f"{path}: strict schemas need additionalProperties: false")
            missing = [p for p in props if p not in required]
            if missing:
                problems.append(f"{path}: strict schemas must require every property; missing {missing}")
        for name, sub in props.items():
            if mcp_names and not PROPERTY_NAME.match(name):
                problems.append(f"{path}/properties/{name}: property name not allowed in MCP tool schemas")
            problems += check_schema(sub, strict, mcp_names, f"{path}/properties/{name}")
    if "array" in types:
        if "items" not in schema:
            problems.append(f"{path}: arrays need 'items'")
        else:
            problems += check_schema(schema["items"], strict, mcp_names, f"{path}/items")
    if "enum" in schema:
        if not isinstance(schema["enum"], list) or not schema["enum"]:
            problems.append(f"{path}: enum must be a non-empty list")
        else:
            for value in schema["enum"]:
                if not _type_ok(value, types):
                    problems.append(f"{path}: enum value {value!r} doesn't match the declared type")
    if "const" in schema and not _type_ok(schema["const"], types):
        problems.append(f"{path}: const value doesn't match the declared type")
    return problems


def check_contract():
    """Problems across all four contract files (empty when fine)."""
    problems = []
    for name in SCHEMA_FILES:
        strict = name.startswith("answer.")
        mcp_names = name == "request.schema.json"
        problems += [f"{name} {p}" for p in check_schema(load(name), strict=strict, mcp_names=mcp_names)]
    return problems


# --- instance validation -----------------------------------------------------------------------------------------

def validate(instance, schema, path="$"):
    """Errors validating instance against a subset schema, as a list of strings (empty when valid)."""
    types = _types(schema) or set()
    if types and not _type_ok(instance, types):
        return [f"{path}: expected {' or '.join(sorted(types))}, got {_type_name(instance)}"]
    errors = []
    if "const" in schema and not _equal(instance, schema["const"]):
        errors.append(f"{path}: must be {schema['const']!r}")
    if "enum" in schema and not any(_equal(instance, v) for v in schema["enum"]):
        errors.append(f"{path}: must be one of {schema['enum']!r}")
    if isinstance(instance, str):
        if len(instance) < schema.get("minLength", 0):
            errors.append(f"{path}: shorter than {schema['minLength']} character(s)")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']} characters")
    if _is_number(instance):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: below the minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: above the maximum {schema['maximum']}")
    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            errors.append(f"{path}: needs at least {schema['minItems']} item(s)")
        if "items" in schema:
            for i, item in enumerate(instance):
                errors += validate(item, schema["items"], f"{path}[{i}]")
    if isinstance(instance, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in instance:
                errors.append(f"{path}: missing required field {name!r}")
        for name, value in instance.items():
            if name in props:
                errors += validate(value, props[name], f"{path}.{name}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unknown field {name!r}")
    return errors


def validate_request(request):
    """Errors in a delegate request: the schema, then the rules the schema can't express."""
    errors = validate(request, load("request.schema.json"))
    if errors:
        return errors
    if not _is_absolute(request["workdir"]):
        errors.append("$.workdir: must be an absolute path")
    if "\n" in request["task"] or "\r" in request["task"]:
        errors.append("$.task: must be a single line")
    if request["type"] == "review":
        for name in ("criteria", "source_of_truth"):
            if name not in request:
                errors.append(f"$: review requests need {name!r}")
    return errors


# state -> error kinds it may carry; None means error must be null.
STATE_ERRORS = {
    "running": None, "done": None, "blocked": None, "turn_limit": None,
    "refused": {"quota_refused"},
    "timed_out": {"timeout"},
    "failed": {"invalid_request", "provider_unavailable", "provider_error", "schema_invalid", "interrupted"},
}


def validate_result(result, task_type=None):
    """Errors in a result: the schema, state/error agreement, and the type-specific answer shape when known."""
    errors = validate(result, load("result.schema.json"))
    if errors:
        return errors
    allowed, error = STATE_ERRORS[result["state"]], result["error"]
    if allowed is None and error is not None:
        errors.append(f"$.error: must be null when state is {result['state']!r}")
    if allowed is not None and (error is None or error["kind"] not in allowed):
        errors.append(f"$.error: state {result['state']!r} needs error.kind in {sorted(allowed)}")
    if result["state"] in ("running", "refused") and result["answer"] is not None:
        errors.append(f"$.answer: must be null when state is {result['state']!r}")
    if result["state"] == "running" and result["timing"]["finished"] is not None:
        errors.append("$.timing.finished: must be null while running")
    if task_type and result["answer"] is not None:
        errors += [e.replace("$", "$.answer", 1) for e in validate(result["answer"], answer_schema(task_type))]
    return errors


# --- helpers -----------------------------------------------------------------------------------------------------

def _types(schema):
    t = schema.get("type")
    if isinstance(t, str):
        return {t}
    if isinstance(t, list) and t and all(isinstance(x, str) for x in t):
        return set(t)
    return None


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _type_name(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    return {float: "number", str: "string", list: "array", dict: "object"}.get(type(v), type(v).__name__)


def _type_ok(v, types):
    name = _type_name(v)
    if name in types:
        return True
    if name == "integer" and "number" in types:
        return True
    return name == "number" and "integer" in types and float(v).is_integer()


def _equal(a, b):
    # JSON equality: true is not 1.
    return type(a) is type(b) and a == b if isinstance(a, bool) or isinstance(b, bool) else a == b


def _is_absolute(p):
    return ntpath.isabs(p) or posixpath.isabs(p)
