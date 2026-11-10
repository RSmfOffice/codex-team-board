from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from collections import deque
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence, TypedDict

from . import __version__
from .processes import (
    ProcessJob,
    process_identity as get_process_identity,
)
from .redaction import redact_sensitive_text


__all__ = [
    "AppServerDisconnectedError",
    "AppServerError",
    "AppServerEvents",
    "AppServerProtocolError",
    "AppServerRPCError",
    "AppServerRuntime",
    "AppServerStartupError",
    "AppServerTimeoutError",
    "TokenBreakdown",
    "TokenUsage",
    "map_token_usage",
]


class AppServerError(RuntimeError):
    """Base error for the private Codex App Server runtime."""


class AppServerStartupError(AppServerError):
    """The App Server process could not complete initialization."""


class AppServerTimeoutError(AppServerError):
    """An App Server JSON-RPC request did not receive a response in time."""


class AppServerDisconnectedError(AppServerError):
    """The App Server process disconnected before answering a request."""


class AppServerProtocolError(AppServerError):
    """The App Server emitted an invalid JSONL-RPC message."""


class AppServerRPCError(AppServerError):
    """The App Server rejected a JSON-RPC request."""

    def __init__(
        self,
        method: str,
        code: int | str | None,
        message: str,
        *,
        data: Any = None,
    ) -> None:
        self.method = method
        self.code = code
        self.data = data
        super().__init__(f"{method} failed ({code}): {message}")


class TokenBreakdown(TypedDict, total=False):
    """One normalized App Server usage bucket."""

    input: int
    cached: int
    uncached: int
    output: int
    context: int


class TokenUsage(TypedDict):
    """Both cumulative and current-turn App Server token snapshots."""

    total: TokenBreakdown | None
    last: TokenBreakdown | None
    modelContextWindow: int | None


class AppServerEvents:
    """Optional callback interface for App Server notifications.

    Callback payloads are mappings decoded from the private JSONL stream. The
    runtime never prints request parameters or stderr, so integrations decide
    whether and how to persist any callback data.
    """

    def on_thread_started(
        self,
        thread_id: str,
        thread: Mapping[str, Any],
    ) -> None:
        """A thread was created or resumed."""

    def on_thread_status_changed(
        self,
        thread_id: str,
        status: Mapping[str, Any],
    ) -> None:
        """A loaded thread changed runtime status."""

    def on_thread_closed(self, thread_id: str) -> None:
        """A thread was unloaded by the App Server."""

    def on_turn_started(
        self,
        thread_id: str,
        turn_id: str,
        turn: Mapping[str, Any],
    ) -> None:
        """A turn entered the in-progress state."""

    def on_turn_completed(
        self,
        thread_id: str,
        turn_id: str,
        turn: Mapping[str, Any],
    ) -> None:
        """A turn reached completed, interrupted, or failed."""

    def on_token_usage_updated(
        self,
        thread_id: str,
        turn_id: str,
        usage: TokenUsage,
    ) -> None:
        """The App Server published normalized token usage."""

    def on_warning(self, message: str, thread_id: str | None) -> None:
        """A non-fatal runtime warning was reported."""

    def on_error(
        self,
        thread_id: str,
        turn_id: str,
        error: Mapping[str, Any],
        will_retry: bool,
    ) -> None:
        """An App Server error notification was reported."""


@dataclass
class _PendingRequest:
    method: str
    future: Future[Any]


@dataclass
class _EventDispatch:
    callback_name: str
    params: dict[str, Any]
    kwargs: dict[str, Any]


def _default_codex_command() -> list[str]:
    configured = os.environ.get("CODEX_TEAM_CODEX_COMMAND", "").strip()
    if configured:
        executable = configured
    else:
        executable = shutil.which("codex.exe") or shutil.which("codex")
        if not executable:
            known = Path.home() / "AppData/Local/Programs/OpenAI/Codex/bin/codex.exe"
            executable = str(known) if known.is_file() else "codex"
    return [
        str(executable),
        "app-server",
        "--listen",
        "stdio://",
    ]


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _optional_non_negative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0, number)


def _map_usage_bucket(value: Any) -> TokenBreakdown | None:
    if not isinstance(value, Mapping):
        return None
    mapped: TokenBreakdown = {}
    input_tokens = _optional_non_negative_int(value.get("inputTokens"))
    if input_tokens is not None:
        mapped["input"] = input_tokens

    cached_tokens = _optional_non_negative_int(
        value.get("cachedInputTokens")
    )
    if cached_tokens is not None:
        if input_tokens is not None:
            cached_tokens = min(cached_tokens, input_tokens)
        mapped["cached"] = cached_tokens

    uncached_tokens = _optional_non_negative_int(
        value.get("uncachedInputTokens")
    )
    if uncached_tokens is not None:
        mapped["uncached"] = uncached_tokens
    elif input_tokens is not None and cached_tokens is not None:
        mapped["uncached"] = input_tokens - cached_tokens

    output_tokens = _optional_non_negative_int(value.get("outputTokens"))
    if output_tokens is not None:
        mapped["output"] = output_tokens
    context_tokens = _optional_non_negative_int(value.get("totalTokens"))
    if context_tokens is not None:
        mapped["context"] = context_tokens
    return mapped


def map_token_usage(payload: Mapping[str, Any]) -> TokenUsage:
    """Preserve both cumulative and per-turn App Server usage.

    `total` is a thread-lifetime snapshot and `last` is the current turn. A
    missing bucket remains ``None`` so consumers can fall back to a cumulative
    delta instead of confusing "missing" with a real zero.
    """

    total = _map_usage_bucket(payload.get("total"))
    last = _map_usage_bucket(payload.get("last"))
    context_window = payload.get("modelContextWindow")
    if context_window is None:
        normalized_window = None
    else:
        normalized_window = _non_negative_int(context_window)
    return {
        "total": total,
        "last": last,
        "modelContextWindow": normalized_window,
    }


def _sandbox_policy(
    sandbox_mode: str | Mapping[str, Any] | None,
    cwd: str | None,
) -> dict[str, Any] | None:
    if sandbox_mode is None:
        return None
    if isinstance(sandbox_mode, Mapping):
        return dict(sandbox_mode)
    policies: dict[str, dict[str, Any]] = {
        "read-only": {"type": "readOnly"},
        "workspace-write": {"type": "workspaceWrite"},
        "danger-full-access": {"type": "dangerFullAccess"},
        "readOnly": {"type": "readOnly"},
        "workspaceWrite": {"type": "workspaceWrite"},
        "dangerFullAccess": {"type": "dangerFullAccess"},
    }
    try:
        policy = dict(policies[sandbox_mode])
    except KeyError as exc:
        raise ValueError(f"unsupported sandbox mode: {sandbox_mode}") from exc
    if policy["type"] == "workspaceWrite" and cwd:
        policy["writableRoots"] = [cwd]
    return policy


class AppServerRuntime:
    """Own a private Codex App Server process over JSONL stdio.

    The default command is the current `codex` executable followed by
    `app-server --listen stdio://`. Each runtime instance owns exactly one
    child process and never connects to the desktop App Server daemon or a
    shared control socket.

    Public event callbacks use the method names and parameters documented by
    `AppServerEvents`. A callable or mapping event handler is also accepted:
    callables receive `(callback_name, params)`, while mappings are keyed by
    callback name and receive the documented keyword arguments. Callbacks run
    serially on a dedicated dispatcher thread so slow handlers cannot block
    JSON-RPC response correlation.
    """

    def __init__(
        self,
        command: Sequence[str] | str | None = None,
        *,
        request_timeout: float = 10.0,
        startup_timeout: float = 10.0,
        stop_timeout: float = 2.0,
        stderr_log_path: str | Path | None = None,
        event_handler: Any = None,
        callbacks: Any = None,
        env: Mapping[str, str] | None = None,
        cwd: str | Path | None = None,
        client_name: str = "codex_team_board",
        client_title: str = "Codex Team Board",
    ) -> None:
        if event_handler is not None and callbacks is not None:
            raise ValueError("pass either event_handler or callbacks, not both")
        if request_timeout <= 0 or startup_timeout <= 0 or stop_timeout <= 0:
            raise ValueError("runtime timeouts must be positive")
        if command is None:
            normalized_command = _default_codex_command()
        elif isinstance(command, str):
            normalized_command = [command]
        else:
            normalized_command = [str(part) for part in command]
        if not normalized_command:
            raise ValueError("command must not be empty")

        self.command = normalized_command
        self.request_timeout = float(request_timeout)
        self.startup_timeout = float(startup_timeout)
        self.stop_timeout = float(stop_timeout)
        self.stderr_log_path = (
            Path(stderr_log_path) if stderr_log_path is not None else None
        )
        self.event_handler = event_handler if event_handler is not None else callbacks
        self._extra_env = dict(env or {})
        self._process_cwd = os.fspath(cwd) if cwd is not None else None
        self._client_info = {
            "name": client_name,
            "title": client_title,
            "version": __version__,
        }

        self._lifecycle_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._process_job: ProcessJob | None = None
        self._process_identity: dict[str, Any] = {}
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._event_queue: queue.Queue[_EventDispatch | None] | None = None
        self._event_thread: threading.Thread | None = None
        self._pending: dict[int | str, _PendingRequest] = {}
        self._next_request_id = 1
        self._generation = 0
        self._restart_count = 0
        self._initialized = False
        self._stopping = False
        self._last_error: str | None = None
        self._last_return_code: int | None = None
        self._stderr_lines: deque[str] = deque(maxlen=500)
        self._callback_errors: deque[str] = deque(maxlen=50)

    @property
    def stderr_lines(self) -> tuple[str, ...]:
        """Recent stderr lines; this method never prints them."""

        with self._state_lock:
            return tuple(self._stderr_lines)

    @property
    def callback_errors(self) -> tuple[str, ...]:
        """Errors raised by user callbacks, isolated from the reader thread."""

        with self._state_lock:
            return tuple(self._callback_errors)

    @property
    def generation(self) -> int:
        with self._state_lock:
            return self._generation

    @property
    def restart_count(self) -> int:
        with self._state_lock:
            return self._restart_count

    def __enter__(self) -> AppServerRuntime:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.stop()

    def start(self) -> dict[str, Any]:
        """Start the private process and complete the JSON-RPC handshake."""

        with self._lifecycle_lock:
            with self._state_lock:
                if self._is_running_locked() and self._initialized:
                    return self.health()
            if self._process is not None:
                self._stop_locked()
            self._start_locked()
            return self.health()

    def stop(self) -> None:
        """Stop the private process without touching any shared daemon."""

        with self._lifecycle_lock:
            self._stop_locked()

    def restart(self) -> dict[str, Any]:
        """Replace the private process and perform a fresh handshake."""

        with self._lifecycle_lock:
            self._stop_locked()
            with self._state_lock:
                self._restart_count += 1
            self._start_locked()
            return self.health()

    def health(self) -> dict[str, Any]:
        """Return a serializable snapshot of this runtime's process state."""

        with self._state_lock:
            process = self._process
            process_running = process is not None and process.poll() is None
            initialized = bool(process_running and self._initialized)
            return {
                "running": process_running,
                "initialized": initialized,
                "healthy": initialized,
                "pid": process.pid if process_running and process is not None else None,
                "process_identity": (
                    dict(self._process_identity)
                    if process_running and self._process_identity
                    else None
                ),
                "job_object": self._process_job is not None,
                "generation": self._generation,
                "restart_count": self._restart_count,
                "last_return_code": self._last_return_code,
                "last_error": self._last_error,
                "stderr_line_count": len(self._stderr_lines),
            }

    def start_thread(
        self,
        cwd: str | Path,
        developer_instructions: str | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        service_name: str | None = None,
    ) -> str:
        """Create a thread and return its stable App Server thread id."""

        self._ensure_started()
        params: dict[str, Any] = {"cwd": os.fspath(cwd)}
        if developer_instructions is not None:
            params["developerInstructions"] = developer_instructions
        if model is not None:
            params["model"] = model
        if model_provider is not None:
            params["modelProvider"] = model_provider
        if service_name is not None:
            params["serviceName"] = service_name
        result = self._request("thread/start", params)
        thread = _mapping(_mapping(result).get("thread"))
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise AppServerProtocolError("thread/start response omitted thread.id")
        return thread_id

    def resume_thread(self, thread_id: str, exclude_turns: bool = True) -> str:
        """Resume a stored thread and return its id."""

        self._ensure_started()
        result = self._request(
            "thread/resume",
            {
                "threadId": thread_id,
                "excludeTurns": bool(exclude_turns),
            },
        )
        thread = _mapping(_mapping(result).get("thread"))
        resumed_id = thread.get("id", thread_id)
        if not isinstance(resumed_id, str) or not resumed_id:
            raise AppServerProtocolError("thread/resume response omitted thread.id")
        return resumed_id

    def start_turn(
        self,
        thread_id: str,
        input_text: str,
        cwd: str | Path | None = None,
        sandbox_mode: str | Mapping[str, Any] | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> dict[str, str]:
        """Start a turn and return `{turn_id, status}`."""

        self._ensure_started()
        cwd_text = os.fspath(cwd) if cwd is not None else None
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": input_text}],
        }
        if cwd_text is not None:
            params["cwd"] = cwd_text
        policy = _sandbox_policy(sandbox_mode, cwd_text)
        if policy is not None:
            params["sandboxPolicy"] = policy
        if model is not None:
            params["model"] = model
        if effort is not None:
            params["effort"] = effort
        result = _mapping(self._request("turn/start", params))
        turn = _mapping(result.get("turn"))
        turn_id = turn.get("id")
        status = turn.get("status")
        if not isinstance(turn_id, str) or not turn_id:
            raise AppServerProtocolError("turn/start response omitted turn.id")
        if not isinstance(status, str) or not status:
            raise AppServerProtocolError("turn/start response omitted turn.status")
        return {"turn_id": turn_id, "status": status}

    def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        """Request interruption of an active turn."""

        self._ensure_started()
        self._request(
            "turn/interrupt",
            {
                "threadId": thread_id,
                "turnId": turn_id,
            },
        )

    def unsubscribe_thread(self, thread_id: str) -> str:
        """Unsubscribe this private connection from a thread's events."""

        self._ensure_started()
        result = _mapping(
            self._request("thread/unsubscribe", {"threadId": thread_id})
        )
        status = result.get("status")
        return status if isinstance(status, str) else "unknown"

    def list_loaded_threads(self) -> list[str]:
        """Return all thread ids loaded by this private process."""

        self._ensure_started()
        thread_ids: list[str] = []
        cursor: str | None = None
        for _ in range(100):
            params: dict[str, Any] = {}
            if cursor is not None:
                params["cursor"] = cursor
            result = _mapping(self._request("thread/loaded/list", params))
            data = result.get("data", [])
            if isinstance(data, list):
                thread_ids.extend(str(item) for item in data)
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return thread_ids
            cursor = next_cursor
        raise AppServerProtocolError("thread/loaded/list exceeded pagination limit")

    def close(self) -> None:
        self.stop()

    def _ensure_started(self) -> None:
        with self._lifecycle_lock:
            with self._state_lock:
                if self._is_running_locked() and self._initialized:
                    return
                had_process = self._process is not None
            if had_process:
                with self._state_lock:
                    self._restart_count += 1
                self._stop_locked()
            self._start_locked()

    def _start_locked(self) -> None:
        if self._is_running_locked():
            return
        with self._state_lock:
            self._last_error = None
            self._last_return_code = None
            self._initialized = False
            self._stopping = False
            generation = self._generation + 1
        process_env = None
        if self._extra_env:
            process_env = os.environ.copy()
            process_env.update(self._extra_env)
        try:
            process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=self._process_cwd,
                env=process_env,
                shell=False,
            )
        except OSError as exc:
            message = f"failed to launch App Server: {exc}"
            with self._state_lock:
                self._last_error = message
            raise AppServerStartupError(message) from exc
        try:
            process_job = ProcessJob.attach(process)
        except Exception as exc:
            try:
                process.kill()
                process.wait(timeout=self.stop_timeout)
            except (OSError, subprocess.TimeoutExpired):
                pass
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is None or stream.closed:
                    continue
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            message = f"failed to assign App Server Job Object: {exc}"
            with self._state_lock:
                self._last_error = message
            raise AppServerStartupError(message) from exc
        process_identity = get_process_identity(process.pid)
        if not process_identity:
            if process_job is not None:
                process_job.close()
            try:
                process.kill()
                process.wait(timeout=self.stop_timeout)
            except (OSError, subprocess.TimeoutExpired):
                pass
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is None or stream.closed:
                    continue
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            message = "failed to verify App Server process identity"
            with self._state_lock:
                self._last_error = message
            raise AppServerStartupError(message)
        if process.stdin is None or process.stdout is None or process.stderr is None:
            if process_job is not None:
                process_job.close()
            process.kill()
            raise AppServerStartupError("App Server stdio pipes were not created")

        with self._state_lock:
            self._generation = generation
            self._process = process
            self._process_job = process_job
            self._process_identity = dict(process_identity)
        event_queue: queue.Queue[_EventDispatch | None] = queue.Queue()
        event_thread = threading.Thread(
            target=self._dispatch_events,
            args=(event_queue,),
            name=f"app-server-events-{generation}",
            daemon=True,
        )
        reader = threading.Thread(
            target=self._read_stdout,
            args=(process, generation),
            name=f"app-server-stdout-{generation}",
            daemon=True,
        )
        stderr_reader = threading.Thread(
            target=self._read_stderr,
            args=(process, generation),
            name=f"app-server-stderr-{generation}",
            daemon=True,
        )
        with self._state_lock:
            self._reader_thread = reader
            self._stderr_thread = stderr_reader
            self._event_queue = event_queue
            self._event_thread = event_thread
        event_thread.start()
        reader.start()
        stderr_reader.start()

        try:
            self._request(
                "initialize",
                {
                    "clientInfo": dict(self._client_info),
                },
                timeout=self.startup_timeout,
            )
            self._send_message({"method": "initialized", "params": {}})
        except Exception as exc:
            with self._state_lock:
                self._last_error = f"{type(exc).__name__}: {exc}"
            self._stop_locked()
            if isinstance(exc, AppServerError):
                raise
            raise AppServerStartupError(
                f"App Server initialization failed: {exc}"
            ) from exc
        with self._state_lock:
            self._initialized = True

    def _stop_locked(self) -> None:
        with self._state_lock:
            process = self._process
            process_job = self._process_job
            self._stopping = True
            self._initialized = False
            self._fail_pending_locked(
                AppServerDisconnectedError("App Server is stopping")
            )
        if process is not None:
            try:
                if process.stdin is not None and not process.stdin.closed:
                    process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
            try:
                process.wait(timeout=self.stop_timeout)
            except subprocess.TimeoutExpired:
                if not (process_job is not None and process_job.terminate()):
                    try:
                        process.terminate()
                    except OSError:
                        pass
                try:
                    process.wait(timeout=self.stop_timeout)
                except subprocess.TimeoutExpired:
                    if not (process_job is not None and process_job.terminate()):
                        try:
                            process.kill()
                        except OSError:
                            pass
                    try:
                        process.wait(timeout=self.stop_timeout)
                    except subprocess.TimeoutExpired:
                        pass
            except OSError:
                pass
        if process_job is not None:
            process_job.close()
        with self._state_lock:
            reader = self._reader_thread
            stderr_reader = self._stderr_thread
            event_queue = self._event_queue
            event_thread = self._event_thread
        current_thread = threading.current_thread()
        for thread in (reader, stderr_reader):
            if thread is not None and thread is not current_thread:
                thread.join(timeout=self.stop_timeout)
        if event_queue is not None:
            event_queue.put(None)
        if event_thread is not None and event_thread is not current_thread:
            event_thread.join(timeout=self.stop_timeout)
        if process is not None:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is None or stream.closed:
                    continue
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        with self._state_lock:
            if self._process is process:
                self._process = None
                self._process_job = None
                self._process_identity = {}
                self._reader_thread = None
                self._stderr_thread = None
            if self._event_queue is event_queue:
                self._event_queue = None
                self._event_thread = None
            self._stopping = False

    def _is_running_locked(self) -> bool:
        with self._state_lock:
            return self._process is not None and self._process.poll() is None

    def _request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        with self._state_lock:
            process = self._process
            if process is None or process.poll() is not None:
                raise AppServerDisconnectedError(
                    f"App Server is not running for {method}"
                )
            request_id = self._next_request_id
            self._next_request_id += 1
            pending = _PendingRequest(method=method, future=Future())
            self._pending[request_id] = pending
        message: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = dict(params)
        try:
            self._send_message(message)
        except Exception:
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise
        wait_seconds = self.request_timeout if timeout is None else timeout
        try:
            return pending.future.result(timeout=wait_seconds)
        except FutureTimeoutError as exc:
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise AppServerTimeoutError(
                f"{method} timed out after {wait_seconds:.3f}s"
            ) from exc
        except AppServerError:
            raise
        except Exception as exc:
            raise AppServerDisconnectedError(
                f"{method} failed because the App Server disconnected"
            ) from exc

    def _send_message(self, message: Mapping[str, Any]) -> None:
        encoded = json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._write_lock:
            with self._state_lock:
                process = self._process
            if process is None or process.stdin is None or process.stdin.closed:
                raise AppServerDisconnectedError("App Server stdin is unavailable")
            try:
                process.stdin.write(encoded + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise AppServerDisconnectedError(
                    "App Server stdin disconnected"
                ) from exc

    def _read_stdout(self, process: subprocess.Popen, generation: int) -> None:
        try:
            if process.stdout is None:
                return
            for line in process.stdout:
                text = line.strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except (TypeError, ValueError) as exc:
                    self._protocol_failure(
                        generation,
                        f"invalid JSON from App Server: {exc}",
                    )
                    continue
                if not isinstance(message, Mapping):
                    self._protocol_failure(
                        generation,
                        "App Server emitted a non-object JSON message",
                    )
                    continue
                self._handle_message(message, generation)
        except (OSError, ValueError) as exc:
            self._protocol_failure(generation, f"App Server stdout failed: {exc}")
        finally:
            self._mark_disconnected(process, generation)
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except (OSError, ValueError):
                    pass

    def _handle_message(
        self,
        message: Mapping[str, Any],
        generation: int,
    ) -> None:
        request_id = message.get("id")
        method = message.get("method")
        if isinstance(method, str) and request_id is not None:
            self._reject_server_request(request_id, method)
            return
        if isinstance(method, str):
            self._dispatch_notification(method, _mapping(message.get("params")))
            return
        if request_id is None:
            self._protocol_failure(
                generation,
                "App Server emitted a message without method or id",
            )
            return
        with self._state_lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            return
        error = message.get("error")
        if isinstance(error, Mapping):
            pending.future.set_exception(
                AppServerRPCError(
                    pending.method,
                    error.get("code"),
                    str(error.get("message", "unknown error")),
                    data=error.get("data"),
                )
            )
            return
        if "result" not in message:
            pending.future.set_exception(
                AppServerProtocolError(
                    f"{pending.method} response omitted result and error"
                )
            )
            return
        pending.future.set_result(message.get("result"))

    def _reject_server_request(self, request_id: Any, method: str) -> None:
        try:
            self._send_message(
                {
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": (
                            f"client request is not supported by this runtime: {method}"
                        ),
                    },
                }
            )
        except AppServerError:
            return

    def _dispatch_notification(
        self,
        method: str,
        params: Mapping[str, Any],
    ) -> None:
        if method == "thread/started":
            thread = _mapping(params.get("thread"))
            thread_id = thread.get("id")
            if isinstance(thread_id, str):
                self._emit(
                    "on_thread_started",
                    params,
                    thread_id=thread_id,
                    thread=thread,
                )
            return
        if method == "thread/status/changed":
            thread_id = params.get("threadId")
            status = _mapping(params.get("status"))
            if isinstance(thread_id, str):
                self._emit(
                    "on_thread_status_changed",
                    params,
                    thread_id=thread_id,
                    status=status,
                )
            return
        if method == "thread/closed":
            thread_id = params.get("threadId")
            if isinstance(thread_id, str):
                self._emit(
                    "on_thread_closed",
                    params,
                    thread_id=thread_id,
                )
            return
        if method in {"turn/started", "turn/completed"}:
            thread_id = params.get("threadId")
            turn = _mapping(params.get("turn"))
            turn_id = turn.get("id")
            if isinstance(thread_id, str) and isinstance(turn_id, str):
                callback = (
                    "on_turn_started"
                    if method == "turn/started"
                    else "on_turn_completed"
                )
                self._emit(
                    callback,
                    params,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    turn=turn,
                )
            return
        if method == "thread/tokenUsage/updated":
            thread_id = params.get("threadId")
            turn_id = params.get("turnId")
            if isinstance(thread_id, str) and isinstance(turn_id, str):
                usage = map_token_usage(_mapping(params.get("tokenUsage")))
                self._emit(
                    "on_token_usage_updated",
                    params,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    usage=usage,
                )
            return
        if method == "warning":
            thread_id = params.get("threadId")
            normalized_thread_id = thread_id if isinstance(thread_id, str) else None
            self._emit(
                "on_warning",
                params,
                message=str(params.get("message", "")),
                thread_id=normalized_thread_id,
            )
            return
        if method == "error":
            thread_id = params.get("threadId")
            turn_id = params.get("turnId")
            if isinstance(thread_id, str) and isinstance(turn_id, str):
                self._emit(
                    "on_error",
                    params,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    error=_mapping(params.get("error")),
                    will_retry=bool(params.get("willRetry", False)),
                )

    def _emit(
        self,
        callback_name: str,
        params: Mapping[str, Any],
        **kwargs: Any,
    ) -> None:
        with self._state_lock:
            event_queue = self._event_queue
        if event_queue is not None:
            event_queue.put(
                _EventDispatch(
                    callback_name=callback_name,
                    params=dict(params),
                    kwargs=dict(kwargs),
                )
            )

    def _dispatch_events(
        self,
        event_queue: queue.Queue[_EventDispatch | None],
    ) -> None:
        while True:
            event = event_queue.get()
            try:
                if event is None:
                    return
                self._invoke_event_handler(
                    event.callback_name,
                    event.params,
                    **event.kwargs,
                )
            finally:
                event_queue.task_done()

    def _invoke_event_handler(
        self,
        callback_name: str,
        params: Mapping[str, Any],
        **kwargs: Any,
    ) -> None:
        with self._state_lock:
            handler = self.event_handler
        if handler is None:
            return
        try:
            callback = getattr(handler, callback_name, None)
            if callable(callback):
                callback(**kwargs)
                return
            if isinstance(handler, Mapping):
                mapped_callback = handler.get(callback_name)
                if callable(mapped_callback):
                    mapped_callback(**kwargs)
                return
            on_event = getattr(handler, "on_event", None)
            if callable(on_event):
                on_event(callback_name, dict(params))
                return
            if callable(handler):
                handler(callback_name, dict(params))
        except Exception as exc:
            with self._state_lock:
                self._callback_errors.append(f"{type(exc).__name__}: {exc}")

    def _protocol_failure(self, generation: int, message: str) -> None:
        with self._state_lock:
            if generation != self._generation:
                return
            self._last_error = message
            self._fail_pending_locked(AppServerProtocolError(message))

    def _mark_disconnected(
        self,
        process: subprocess.Popen,
        generation: int,
    ) -> None:
        process_job: ProcessJob | None = None
        with self._state_lock:
            if process is not self._process or generation != self._generation:
                return
            process_job = self._process_job
            self._process_job = None
            return_code = process.poll()
            self._last_return_code = return_code
            self._initialized = False
            if not self._stopping:
                self._last_error = (
                    f"App Server disconnected with exit code {return_code}"
                )
            self._fail_pending_locked(
                AppServerDisconnectedError("App Server disconnected")
            )
        if process_job is not None:
            process_job.close()

    def _fail_pending_locked(self, error: AppServerError) -> None:
        pending = list(self._pending.values())
        self._pending.clear()
        for request in pending:
            if not request.future.done():
                request.future.set_exception(error)

    def _read_stderr(self, process: subprocess.Popen, generation: int) -> None:
        log_stream = None
        try:
            if self.stderr_log_path is not None:
                self.stderr_log_path.parent.mkdir(parents=True, exist_ok=True)
                log_stream = self.stderr_log_path.open(
                    "a",
                    encoding="utf-8",
                    errors="replace",
                )
            if process.stderr is None:
                return
            for line in process.stderr:
                text = line.rstrip("\r\n")
                with self._state_lock:
                    self._stderr_lines.append(text)
                if log_stream is not None:
                    log_stream.write(redact_sensitive_text(text) + "\n")
                    log_stream.flush()
        except OSError as exc:
            with self._state_lock:
                if generation == self._generation:
                    self._last_error = f"App Server stderr capture failed: {exc}"
        finally:
            if log_stream is not None:
                try:
                    log_stream.close()
                except OSError:
                    pass
            if process.stderr is not None:
                try:
                    process.stderr.close()
                except OSError:
                    pass
