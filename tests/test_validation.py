from __future__ import annotations

import unittest

from codex_team.validation import SchemaValidationError, validate_input_schema


class ValidationTests(unittest.TestCase):
    def test_object_schema_validates_required_types_and_enums(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "actor": {"type": "string", "minLength": 1},
                "approve": {"type": "boolean"},
                "priority": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 3,
                },
                "mode": {"type": "string", "enum": ["auto", "fresh"]},
                "depends_on": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["actor", "approve"],
            "additionalProperties": False,
        }
        validate_input_schema(
            {
                "actor": "lead",
                "approve": True,
                "priority": 1,
                "mode": "auto",
                "depends_on": ["T-1"],
            },
            schema,
        )

        invalid_values = (
            {"actor": "lead"},
            {"actor": "lead", "approve": "false"},
            {"actor": "lead", "approve": True, "priority": True},
            {"actor": "lead", "approve": True, "priority": 4},
            {"actor": "lead", "approve": True, "mode": "invalid"},
            {"actor": "lead", "approve": True, "depends_on": ["T-1", 2]},
            {"actor": "lead", "approve": True, "unexpected": 1},
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(SchemaValidationError):
                    validate_input_schema(value, schema)

    def test_min_length_and_type_errors_are_actionable(self) -> None:
        with self.assertRaisesRegex(SchemaValidationError, "arguments.reason"):
            validate_input_schema(
                {"reason": ""},
                {
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string", "minLength": 1},
                    },
                },
            )
        with self.assertRaisesRegex(SchemaValidationError, "must be of type boolean"):
            validate_input_schema(
                "false",
                {"type": "boolean"},
            )


if __name__ == "__main__":
    unittest.main()
