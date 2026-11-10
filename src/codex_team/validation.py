from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class SchemaValidationError(ValueError):
    """Raised when a value does not satisfy a supported JSON schema subset."""


def validate_input_schema(
    value: Any,
    schema: Mapping[str, Any],
    *,
    path: str = "arguments",
) -> None:
    """Validate the JSON schema keywords used by Team Board tool definitions."""

    expected_type = schema.get("type")
    if expected_type is not None:
        _validate_type(value, str(expected_type), path)

    enum = schema.get("enum")
    if isinstance(enum, Sequence) and not isinstance(enum, (str, bytes)):
        if value not in enum:
            raise SchemaValidationError(
                f"{path} must be one of: " + ", ".join(map(str, enum))
            )

    if isinstance(value, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            raise SchemaValidationError(
                f"{path} must contain at least {min_length} characters"
            )

    if isinstance(value, int) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, int) and value < minimum:
            raise SchemaValidationError(f"{path} must be at least {minimum}")
        if isinstance(maximum, int) and value > maximum:
            raise SchemaValidationError(f"{path} must be at most {maximum}")

    if isinstance(value, Mapping):
        properties = schema.get("properties")
        property_schemas = properties if isinstance(properties, Mapping) else {}
        required = schema.get("required")
        if isinstance(required, Sequence) and not isinstance(required, (str, bytes)):
            missing = [
                str(name)
                for name in required
                if name not in value
            ]
            if missing:
                raise SchemaValidationError(
                    f"{path} is missing required fields: " + ", ".join(missing)
                )

        additional = schema.get("additionalProperties")
        unknown = [
            str(name)
            for name in value
            if name not in property_schemas
        ]
        if unknown and additional is False:
            raise SchemaValidationError(
                f"{path} contains unsupported fields: " + ", ".join(sorted(unknown))
            )

        for name, item in value.items():
            item_schema = property_schemas.get(name)
            if isinstance(item_schema, Mapping):
                validate_input_schema(
                    item,
                    item_schema,
                    path=f"{path}.{name}",
                )
            elif isinstance(additional, Mapping):
                validate_input_schema(
                    item,
                    additional,
                    path=f"{path}.{name}",
                )

    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                validate_input_schema(
                    item,
                    items,
                    path=f"{path}[{index}]",
                )


def _validate_type(value: Any, expected: str, path: str) -> None:
    valid = {
        "null": value is None,
        "object": isinstance(value, Mapping),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
        ),
        "boolean": isinstance(value, bool),
    }.get(expected)
    if valid is None:
        raise SchemaValidationError(f"{path} uses unsupported schema type: {expected}")
    if not valid:
        raise SchemaValidationError(f"{path} must be of type {expected}")
