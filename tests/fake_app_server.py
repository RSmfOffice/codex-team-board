from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any


os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PYTHONUTF8"] = "1"

_WRITE_LOCK = threading.Lock()


def send(message: dict[str, Any]) -> None:
    with _WRITE_LOCK:
        sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def trace(message: dict[str, Any]) -> None:
    trace_path = os.environ.get("FAKE_APP_SERVER_TRACE", "")
    if not trace_path:
        return
    path = Path(trace_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(message, ensure_ascii=False) + "\n")


def main() -> int:
    stderr_message = os.environ.get("FAKE_APP_SERVER_STDERR", "")
    if stderr_message:
        sys.stderr.write(stderr_message + "\n")
        sys.stderr.flush()

    initialized = False
    threads: set[str] = set()
    sequence = 0
    turns: dict[str, dict[str, Any]] = {}

    for raw_line in sys.stdin:
        text = raw_line.strip()
        if not text:
            continue
        message = json.loads(text)
        trace(message)
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            send(
                {
                    "id": request_id,
                    "result": {
                        "userAgent": "fake-app-server/1",
                        "platformFamily": "windows",
                        "platformOs": "windows",
                    },
                }
            )
            continue
        if method == "initialized":
            initialized = True
            continue
        if not initialized:
            if request_id is not None:
                send(
                    {
                        "id": request_id,
                        "error": {
                            "code": -32600,
                            "message": "Not initialized",
                        },
                    }
                )
            continue

        if method == "thread/start":
            sequence += 1
            thread_id = f"thread-{sequence}"
            threads.add(thread_id)
            thread = {
                "id": thread_id,
                "sessionId": thread_id,
                "ephemeral": False,
                "status": {"type": "idle"},
            }
            send({"id": request_id, "result": {"thread": thread}})
            send({"method": "thread/started", "params": {"thread": thread}})
            continue

        if method == "thread/resume":
            thread_id = str(params.get("threadId", ""))
            crash_path = os.environ.get("FAKE_APP_SERVER_CRASH_ONCE", "")
            if thread_id == "crash" and crash_path and not Path(crash_path).exists():
                Path(crash_path).parent.mkdir(parents=True, exist_ok=True)
                Path(crash_path).touch()
                sys.exit(17)
            if thread_id == "hang":
                continue
            if thread_id == "missing":
                send(
                    {
                        "id": request_id,
                        "error": {
                            "code": -32602,
                            "message": "thread not found",
                            "data": {"type": "notFound"},
                        },
                    }
                )
                continue
            threads.add(thread_id)
            thread = {
                "id": thread_id,
                "sessionId": thread_id,
                "ephemeral": False,
                "status": {"type": "idle"},
            }
            if thread_id.startswith("slow-"):
                def respond(
                    pending_id: Any = request_id,
                    pending_thread: dict[str, Any] = thread,
                ) -> None:
                    send({"id": pending_id, "result": {"thread": pending_thread}})
                    send(
                        {
                            "method": "thread/started",
                            "params": {"thread": pending_thread},
                        }
                    )

                threading.Timer(0.2, respond).start()
                continue
            send({"id": request_id, "result": {"thread": thread}})
            send({"method": "thread/started", "params": {"thread": thread}})
            continue

        if method == "turn/start":
            thread_id = str(params.get("threadId", ""))
            sequence += 1
            turn_id = f"turn-{sequence}"
            turn = {
                "id": turn_id,
                "status": "inProgress",
                "items": [],
                "error": None,
            }
            turns[turn_id] = turn
            send({"id": request_id, "result": {"turn": turn}})
            send(
                {
                    "method": "thread/status/changed",
                    "params": {
                        "threadId": thread_id,
                        "status": {"type": "active", "activeFlags": []},
                    },
                }
            )
            send(
                {
                    "method": "turn/started",
                    "params": {"threadId": thread_id, "turn": turn},
                }
            )
            input_text = ""
            for item in params.get("input", []):
                if isinstance(item, dict) and item.get("type") == "text":
                    input_text += str(item.get("text", ""))
            if input_text == "usage-overflow":
                input_tokens = 10
                cached_tokens = 15
                last_input_tokens = 8
                last_cached_tokens = 12
            else:
                input_tokens = 120
                cached_tokens = 20
                last_input_tokens = 50
                last_cached_tokens = 5
            send(
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "tokenUsage": {
                            "total": {
                                "inputTokens": input_tokens,
                                "cachedInputTokens": cached_tokens,
                                "outputTokens": 30,
                                "reasoningOutputTokens": 4,
                                "totalTokens": input_tokens + 30,
                            },
                            "last": {
                                "inputTokens": last_input_tokens,
                                "cachedInputTokens": last_cached_tokens,
                                "outputTokens": 10,
                                "reasoningOutputTokens": 1,
                                "totalTokens": last_input_tokens + 10,
                            },
                            "modelContextWindow": 200,
                        },
                    },
                }
            )
            send(
                {
                    "method": "warning",
                    "params": {
                        "threadId": thread_id,
                        "message": "fake warning",
                    },
                }
            )
            send(
                {
                    "method": "error",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "willRetry": False,
                        "error": {
                            "message": "fake nonfatal error",
                            "codexErrorInfo": "other",
                            "additionalDetails": None,
                        },
                    },
                }
            )
            send(
                {
                    "method": "thread/status/changed",
                    "params": {
                        "threadId": thread_id,
                        "status": {"type": "idle"},
                    },
                }
            )
            completed = dict(turn)
            completed["status"] = "completed"
            send(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread_id,
                        "turn": completed,
                    },
                }
            )
            continue

        if method == "turn/interrupt":
            thread_id = str(params.get("threadId", ""))
            turn_id = str(params.get("turnId", ""))
            send({"id": request_id, "result": {}})
            interrupted = dict(turns.get(turn_id, {"id": turn_id, "items": []}))
            interrupted["status"] = "interrupted"
            interrupted.setdefault("error", None)
            send(
                {
                    "method": "thread/status/changed",
                    "params": {
                        "threadId": thread_id,
                        "status": {"type": "idle"},
                    },
                }
            )
            send(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread_id,
                        "turn": interrupted,
                    },
                }
            )
            continue

        if method == "thread/unsubscribe":
            thread_id = str(params.get("threadId", ""))
            status = "unsubscribed" if thread_id in threads else "notLoaded"
            threads.discard(thread_id)
            send({"id": request_id, "result": {"status": status}})
            if status == "unsubscribed":
                send(
                    {
                        "method": "thread/status/changed",
                        "params": {
                            "threadId": thread_id,
                            "status": {"type": "notLoaded"},
                        },
                    }
                )
                send(
                    {
                        "method": "thread/closed",
                        "params": {"threadId": thread_id},
                    }
                )
            continue

        if method == "thread/loaded/list":
            send(
                {
                    "id": request_id,
                    "result": {
                        "data": sorted(threads),
                        "nextCursor": None,
                    },
                }
            )
            continue

        if request_id is not None:
            send(
                {
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"unknown method: {method}",
                    },
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
