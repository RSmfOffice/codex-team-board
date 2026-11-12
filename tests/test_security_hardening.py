"""Regression tests for MCP secret redaction and frame size hard limits."""

from __future__ import annotations

import io
import json
import logging
import unittest
from unittest.mock import patch

from codex_team.mcp import (
    MAX_MCP_MESSAGE_BYTES,
    TeamMcpServer,
    _process,
    _read_message,
)
from codex_team.service import TeamError


class _CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class McpLogRedactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = TeamMcpServer.__new__(TeamMcpServer)
        self.secret = "claim-token-SECRET-9f8e7d6c5b4a"

    def _run_with_error(self, exc: Exception) -> tuple[dict, str]:
        handler = _CaptureHandler()
        logger = logging.getLogger("codex_team.mcp")
        logger.addHandler(handler)
        previous_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            with patch.object(self.server, "call_tool", side_effect=exc):
                response = _process(
                    self.server,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "team_read",
                            "arguments": {"operation": "team_status"},
                        },
                    },
                )
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
        logged = "\n".join(record.getMessage() for record in handler.records)
        return response, logged

    def test_team_error_log_and_response_are_redacted(self) -> None:
        response, logged = self._run_with_error(
            TeamError(f"claim_token={self.secret} rejected")
        )
        self.assertNotIn(self.secret, logged)
        self.assertIn("[REDACTED]", logged)
        text = response["result"]["content"][0]["text"]
        self.assertNotIn(self.secret, text)
        self.assertIn("[REDACTED]", text)

    def test_unexpected_error_log_and_response_are_redacted(self) -> None:
        response, logged = self._run_with_error(
            RuntimeError(f"Authorization: Bearer {self.secret}")
        )
        self.assertNotIn(self.secret, logged)
        text = response["result"]["content"][0]["text"]
        self.assertNotIn(self.secret, text)


class McpFrameLimitTests(unittest.TestCase):
    def test_valid_jsonl_line(self) -> None:
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        message = _read_message(io.BytesIO(payload.encode("utf-8") + b"\n"))
        self.assertEqual(message["method"], "ping")

    def test_valid_content_length_frame(self) -> None:
        payload = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        raw = (
            f"Content-Length: {len(payload.encode('utf-8'))}\r\n\r\n{payload}"
        ).encode("utf-8")
        message = _read_message(io.BytesIO(raw))
        self.assertEqual(message["id"], 2)

    def test_oversized_jsonl_line_rejected(self) -> None:
        # A single line one byte over the cap, with no newline, must be refused
        # rather than buffered without bound.
        oversized = b"x" * (MAX_MCP_MESSAGE_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "exceeds"):
            _read_message(io.BytesIO(oversized))

    def test_jsonl_at_cap_is_accepted(self) -> None:
        # Exactly the cap (a valid JSON string payload of that length) parses.
        body = '"' + "a" * (MAX_MCP_MESSAGE_BYTES - 2) + '"'
        self.assertEqual(len(body), MAX_MCP_MESSAGE_BYTES)
        message = _read_message(io.BytesIO(body.encode("utf-8")))
        self.assertEqual(message, "a" * (MAX_MCP_MESSAGE_BYTES - 2))

    def test_jsonl_at_cap_with_newline_is_accepted(self) -> None:
        # Normal JSONL terminates with a newline; the framing newline must not
        # count against the body cap, so a max-size body + "\n" is accepted.
        body = '"' + "a" * (MAX_MCP_MESSAGE_BYTES - 2) + '"'
        self.assertEqual(len(body), MAX_MCP_MESSAGE_BYTES)
        message = _read_message(io.BytesIO(body.encode("utf-8") + b"\n"))
        self.assertEqual(message, "a" * (MAX_MCP_MESSAGE_BYTES - 2))

    def test_jsonl_at_cap_with_crlf_is_accepted(self) -> None:
        body = '"' + "a" * (MAX_MCP_MESSAGE_BYTES - 2) + '"'
        message = _read_message(io.BytesIO(body.encode("utf-8") + b"\r\n"))
        self.assertEqual(message, "a" * (MAX_MCP_MESSAGE_BYTES - 2))

    def test_oversized_jsonl_with_newline_rejected(self) -> None:
        # A body one byte over the cap is refused even with a trailing newline.
        oversized = b"x" * (MAX_MCP_MESSAGE_BYTES + 1) + b"\n"
        with self.assertRaisesRegex(ValueError, "exceeds"):
            _read_message(io.BytesIO(oversized))

    def test_oversized_content_length_rejected(self) -> None:
        raw = f"Content-Length: {MAX_MCP_MESSAGE_BYTES + 1}\r\n\r\n".encode(
            "utf-8"
        )
        with self.assertRaisesRegex(ValueError, "Content-Length"):
            _read_message(io.BytesIO(raw))

    def test_invalid_content_length_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Content-Length"):
            _read_message(io.BytesIO(b"Content-Length: abc\r\n\r\n"))

    def test_truncated_payload_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "truncated"):
            _read_message(io.BytesIO(b"Content-Length: 100\r\n\r\n{}"))


if __name__ == "__main__":
    unittest.main()