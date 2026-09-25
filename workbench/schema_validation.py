#!/usr/bin/env python3
"""Small standard-library JSON Schema subset validator."""
from __future__ import annotations

import re
from typing import Any


class SchemaValidationError(ValueError):
    pass


SchemaError = SchemaValidationError


SUPPORTED = {
    "type", "enum", "const", "pattern", "minLength", "required",
    "additionalProperties", "propertyNames", "items", "uniqueItems",
    "$defs", "$ref", "properties", "minItems",
    "$schema", "$id", "$comment", "title", "description", "default", "examples",
}
_TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})
_STRING_ANNOTATIONS = frozenset({"$schema", "$id", "$comment", "title", "description"})


def _schema_error(path: str, message: str) -> SchemaValidationError:
    return SchemaValidationError(f"{path}: {message}")


def _non_negative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _json_equal(left: object, right: object) -> bool:
    """JSON equality keeps booleans distinct from numeric values."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, dict):
        return (
            set(left) == set(right)
            and all(_json_equal(left[key], right[key]) for key in left)
        )
    return left == right


def _validate_schema_shape(schema: object, path: str, seen: set[int] | None = None) -> None:
    if not isinstance(schema, dict):
        raise _schema_error(path, "schema must be object")
    seen = set() if seen is None else seen
    identity = id(schema)
    if identity in seen:
        raise _schema_error(path, "schema may not contain cycles")
    seen.add(identity)
    try:
        unsupported = set(schema) - SUPPORTED
        if unsupported:
            names = ", ".join(sorted(str(item) for item in unsupported))
            raise _schema_error(path, f"unsupported schema keyword(s): {names}")
        if not all(isinstance(key, str) for key in schema):
            raise _schema_error(path, "schema keyword names must be strings")
        for key in _STRING_ANNOTATIONS & set(schema):
            if not isinstance(schema[key], str):
                raise _schema_error(path, f"{key} must be string")
        if "examples" in schema and not isinstance(schema["examples"], list):
            raise _schema_error(path, "examples must be array")
        if "$ref" in schema and not isinstance(schema["$ref"], str):
            raise _schema_error(path, "$ref must be string")
        if "type" in schema:
            raw_type = schema["type"]
            types = [raw_type] if isinstance(raw_type, str) else raw_type
            if (
                not isinstance(types, list)
                or not types
                or any(not isinstance(item, str) or item not in _TYPES for item in types)
            ):
                raise _schema_error(path, "type must be a known string or non-empty array")
        if "enum" in schema and not isinstance(schema["enum"], list):
            raise _schema_error(path, "enum must be array")
        if "pattern" in schema and not isinstance(schema["pattern"], str):
            raise _schema_error(path, "pattern must be string")
        for key in ("minLength", "minItems"):
            if key in schema and not _non_negative_integer(schema[key]):
                raise _schema_error(path, f"{key} must be non-negative integer")
        if "required" in schema:
            required = schema["required"]
            if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
                raise _schema_error(path, "required must be string array")
        if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
            raise _schema_error(path, "uniqueItems must be boolean")
        if "properties" in schema:
            properties = schema["properties"]
            if not isinstance(properties, dict) or not all(isinstance(name, str) for name in properties):
                raise _schema_error(path, "properties must be object")
            for name, child in properties.items():
                _validate_schema_shape(child, f"{path}.properties.{name}", seen)
        if "propertyNames" in schema:
            _validate_schema_shape(schema["propertyNames"], f"{path}.propertyNames", seen)
        if "items" in schema:
            _validate_schema_shape(schema["items"], f"{path}.items", seen)
        if "additionalProperties" in schema:
            additional = schema["additionalProperties"]
            if not isinstance(additional, (bool, dict)):
                raise _schema_error(path, "additionalProperties must be boolean or object")
            if isinstance(additional, dict):
                _validate_schema_shape(additional, f"{path}.additionalProperties", seen)
        if "$defs" in schema:
            definitions = schema["$defs"]
            if not isinstance(definitions, dict) or not all(isinstance(name, str) for name in definitions):
                raise _schema_error(path, "$defs must be object")
            for name, child in definitions.items():
                _validate_schema_shape(child, f"{path}.$defs.{name}", seen)
    finally:
        seen.remove(identity)


def validate(
    instance: Any,
    schema: dict[str, Any],
    *,
    path: str = "$",
    root: dict[str, Any] | None = None,
    _ref_stack: tuple[str, ...] = (),
) -> None:
    if root is None:
        _validate_schema_shape(schema, path)
        root = schema
    try:
        _validate(instance, schema, path=path, root=root, ref_stack=_ref_stack)
    except SchemaValidationError:
        raise
    except RecursionError as exc:
        raise _schema_error(path, "schema reference recursion") from exc
    except (AttributeError, KeyError, TypeError) as exc:
        raise _schema_error(path, "invalid schema structure") from exc


def _validate(
    instance: Any,
    schema: dict[str, Any],
    *,
    path: str,
    root: dict[str, Any],
    ref_stack: tuple[str, ...],
) -> None:
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/$defs/"):
            raise _schema_error(path, "only local $defs refs are supported")
        if ref in ref_stack:
            raise _schema_error(path, f"cyclic ref {ref}")
        name = ref[len("#/$defs/"):]
        definitions = root.get("$defs", {})
        if name not in definitions:
            raise _schema_error(path, f"unresolved ref {ref}")
        validate(
            instance,
            definitions[name],
            path=path,
            root=root,
            _ref_stack=(*ref_stack, ref),
        )
        siblings = {key: value for key, value in schema.items() if key != "$ref"}
        if siblings:
            _validate(instance, siblings, path=path, root=root, ref_stack=ref_stack)
        return
    if "const" in schema and not _json_equal(instance, schema["const"]):
        raise _schema_error(path, "value does not match const")
    if "enum" in schema and not any(
        _json_equal(instance, candidate) for candidate in schema["enum"]
    ):
        raise _schema_error(path, "value is not in enum")
    if "type" in schema:
        expected = schema["type"]
        expected_types = [expected] if isinstance(expected, str) else expected
        if not any(_matches_type(instance, item) for item in expected_types):
            raise _schema_error(path, f"expected {expected}")
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            raise _schema_error(path, "string is too short")
        if "pattern" in schema:
            try:
                matched = re.search(schema["pattern"], instance)
            except re.error as exc:
                raise _schema_error(path, f"invalid pattern: {exc}") from exc
            if not matched:
                raise _schema_error(path, "string does not match pattern")
    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for key in required:
            if key not in instance:
                raise _schema_error(path, f"missing required property {key}")
        additional = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if "propertyNames" in schema:
                validate(
                    key,
                    schema["propertyNames"],
                    path=f"{path}.<name>",
                    root=root,
                    _ref_stack=ref_stack,
                )
            if key in properties:
                validate(
                    value,
                    properties[key],
                    path=f"{path}.{key}",
                    root=root,
                    _ref_stack=ref_stack,
                )
            elif additional is False:
                raise _schema_error(path, f"unknown property {key}")
            elif isinstance(additional, dict):
                validate(
                    value,
                    additional,
                    path=f"{path}.{key}",
                    root=root,
                    _ref_stack=ref_stack,
                )
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise _schema_error(path, "array is too short")
        if "items" in schema:
            for index, value in enumerate(instance):
                validate(
                    value,
                    schema["items"],
                    path=f"{path}[{index}]",
                    root=root,
                    _ref_stack=ref_stack,
                )
        if schema.get("uniqueItems"):
            seen = []
            for value in instance:
                if any(_json_equal(value, prior) for prior in seen):
                    raise _schema_error(path, "array items are not unique")
                seen.append(value)


def _matches_type(value: Any, expected: str) -> bool:
    return {
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        "boolean": lambda: isinstance(value, bool),
        "null": lambda: value is None,
    }[expected]()


def validate_schema(instance: Any, schema: dict[str, Any]) -> None:
    validate(instance, schema)


def validate_instance(instance: Any, schema: dict[str, Any]) -> None:
    validate(instance, schema)
