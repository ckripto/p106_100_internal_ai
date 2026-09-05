"""Reusable structured-tool runtime for Executor and Developer."""

import json
import os
import selectors
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .events import emit_message
from .protocol import (
    ProtocolError,
    TransportError,
    TransportSettings,
    TransportTimeout,
    clip,
    object_json,
    request,
    string,
)


@dataclass(frozen=True)
class AgentSettings:
    actor: str
    name: str
    root: Path
    prompt_path: Path
    transport: TransportSettings
    attempt_timeout: float
    command_timeout: float = 30
    step_limit: int = 60
    chunk_size: int = 600
    staging_limit: int = 128_000
    pending_file_limit: int = 4
    task_limit: int = 1800
    snapshot_limit: int = 3000
    instruction_paths: tuple[Path, ...] = ()
    excluded_names: frozenset[str] = field(default_factory=frozenset)
    redundant_path_prefix: str | None = None
    ssh_target: str | None = None
    ssh_connect_timeout: int = 10


def _tool(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def build_tools(chunk_size):
    text = {"type": "string"}
    reason = {"type": "string", "maxLength": 200}
    return [
        _tool(
            "write_file",
            f"Stage a file in chunks <={chunk_size} characters. Start offset=0; "
            "subsequent offsets are accepted character counts. final=true atomically publishes it.",
            {
                "path": text,
                "content": {"type": "string", "maxLength": chunk_size},
                "offset": {"type": "integer", "minimum": 0},
                "final": {"type": "boolean"},
                "reason": reason,
            },
            ["path", "content", "offset", "final"],
        ),
        _tool("run_command", "Run a short shell command from the configured root.", {"command": text, "reason": reason}, ["command"]),
        _tool(
            "read_file",
            f"Read up to {chunk_size} characters of a published or staged file from offset.",
            {"path": text, "offset": {"type": "integer", "minimum": 0}, "reason": reason},
            ["path", "offset"],
        ),
        _tool("list_files", "List files below the configured root (bounded).", {"reason": reason}, []),
        _tool(
            "finish",
            "Finish only after inspecting tool results. Pending writes or unresolved failures reject success.",
            {
                "status": {"type": "string", "enum": ["success", "failed"]},
                "summary": text,
                "reason": reason,
            },
            ["status", "summary"],
        ),
    ]


class ToolState:
    def __init__(self, settings, deadline=None):
        self.settings = settings
        self.deadline = deadline
        self.pending = {}
        self.files = []
        self.commands = []
        self.failures = set()
        self.receipts = []
        self.effects = 0

    def prepare(self):
        self.settings.root.mkdir(parents=True, exist_ok=True)

    def safe_path(self, filename):
        if not isinstance(filename, str) or not filename or len(filename) > 240:
            raise ProtocolError("Invalid path")
        root = self.settings.root.resolve()
        prefix = self.settings.redundant_path_prefix
        if prefix and Path(filename).parts[:1] == (prefix,):
            raise ProtocolError(
                f"Paths are already relative to {root}; remove the {prefix}/ prefix"
            )
        path = (root / filename).resolve()
        if not path.is_relative_to(root) or path == root:
            raise ProtocolError("Path must be inside the configured root")
        return path

    def path_key(self, filename):
        path = self.safe_path(filename)
        return str(path.relative_to(self.settings.root.resolve()))

    def _write_file(self, relative_path, content):
        target = self.safe_path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=target.parent, delete=False
            ) as stream:
                temporary_name = stream.name
                stream.write(content)
            os.replace(temporary_name, target)
        finally:
            if temporary_name and os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def _read_file(self, relative_path, offset):
        with self.safe_path(relative_path).open(encoding="utf-8") as stream:
            stream.read(offset)
            return stream.read(self.settings.chunk_size + 1)

    def _run_process(self, arguments, cwd):
        process = subprocess.Popen(
            arguments,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        remaining = self.settings.command_timeout
        if self.deadline is not None:
            remaining = min(remaining, max(0, self.deadline - time.monotonic()))
        deadline = time.monotonic() + remaining
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        totals = {"stdout": 0, "stderr": 0}
        timed_out = False
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            try:
                while selector.get_map() or process.poll() is None:
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
                    for key, _ in selector.select(timeout=0.1):
                        chunk = os.read(key.fileobj.fileno(), 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        name = key.data
                        totals[name] += len(chunk)
                        buffers[name].extend(chunk[: max(0, 1200 - len(buffers[name]))])
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                process.stdout.close()
                process.stderr.close()
        return {
            "success": not timed_out and process.returncode == 0,
            "returncode": process.returncode,
            "timed_out": timed_out,
            **{
                key: clip(value.decode("utf-8", errors="replace"), 600)
                for key, value in buffers.items()
            },
            "output_truncated": any(totals[key] > 600 for key in totals),
        }

    def _run_command(self, command):
        return self._run_process(
            ["/bin/bash", "-o", "pipefail", "-c", command],
            self.settings.root,
        )

    def _list_files(self):
        files = []
        for directory, child_directories, names in os.walk(self.settings.root):
            child_directories[:] = sorted(
                name for name in child_directories if name not in self.settings.excluded_names
            )
            for name in sorted(names):
                path = Path(directory, name)
                files.append(str(path.relative_to(self.settings.root)))
                if len(files) == 41:
                    return {"success": True, "files": files[:40], "truncated": True}
        return {"success": True, "files": files, "truncated": False}

    def execute(self, data):
        name = data.get("tool")
        arguments = data.get("arguments")
        spec = next(
            (item["function"] for item in build_tools(self.settings.chunk_size)
             if item["function"]["name"] == name),
            None,
        )
        if not spec or name == "finish" or not isinstance(arguments, dict):
            raise ProtocolError("Unknown tool or invalid arguments")
        required = set(spec["parameters"]["required"])
        allowed = set(spec["parameters"]["properties"])
        if not required.issubset(arguments) or not set(arguments).issubset(allowed):
            raise ProtocolError("Unexpected or missing tool arguments")
        if name == "run_command":
            command = string(arguments, "command", 600)
            result = self._run_command(command)
            self.commands.append({"command": command, **result})
            self.effects += 1
            return result
        if name == "list_files":
            self.effects += 1
            return self._list_files()

        key = self.path_key(string(arguments, "path", 240))
        offset = arguments.get("offset")
        if type(offset) is not int or offset < 0:
            raise ProtocolError("offset must be a nonnegative integer")
        if name == "read_file":
            if offset > self.settings.staging_limit:
                raise ProtocolError("Read offset exceeds limit")
            if key in self.pending:
                content = self.pending[key][offset:offset + self.settings.chunk_size + 1]
            else:
                content = self._read_file(key, offset)
            self.effects += 1
            return {
                "success": True,
                "path": key,
                "content": content[:self.settings.chunk_size],
                "next_offset": offset + min(len(content), self.settings.chunk_size),
                "eof": len(content) <= self.settings.chunk_size,
            }

        content = string(arguments, "content", self.settings.chunk_size, allow_empty=True)
        if type(arguments["final"]) is not bool:
            raise ProtocolError("final must be boolean")
        if key not in self.pending and len(self.pending) >= self.settings.pending_file_limit:
            raise ProtocolError("Publish pending files before starting another file")
        previous = self.pending.get(key, "")
        if offset != len(previous):
            raise ProtocolError(f"Wrong offset; expected {len(previous)}")
        if sum(map(len, self.pending.values())) + len(content) > self.settings.staging_limit:
            raise ProtocolError("Staging limit exceeded")
        combined = previous + content
        if arguments["final"]:
            self._write_file(key, combined)
            self.pending.pop(key, None)
            if key not in self.files:
                self.files.append(key)
            self.effects += 1
        else:
            self.pending[key] = combined
        return {
            "success": True,
            "path": key,
            "next_offset": len(combined),
            "published": arguments["final"],
        }

    def result(self, status, summary, timed_out=False):
        if status == "success" and (not self.effects or self.pending or self.failures):
            status = "failed"
            summary = "Success rejected: no execution evidence, pending writes or unresolved tool errors"
        return {
            "type": "result",
            "status": status,
            "summary": clip(summary),
            "timed_out": timed_out,
            "files": self.files,
            "commands": self.commands[-6:],
            "result": {
                "pending_files": list(self.pending),
                "unresolved_errors": len(self.failures),
                "failed_tools": sorted(self.failures)[-6:],
            },
        }


class ToolAgent:
    def __init__(self, settings, state_class=ToolState):
        self.settings = settings
        self.state_class = state_class
        self.tools = build_tools(settings.chunk_size)
        self.system_prompt = self._load_system_prompt()

    def _load_system_prompt(self):
        parts = [self.settings.prompt_path.read_text(encoding="utf-8").strip()]
        for path in self.settings.instruction_paths:
            parts.append(f"\nProject instructions from {path}:\n{path.read_text(encoding='utf-8').strip()}")
        return "\n".join(parts)

    def ask_llm(self, messages, response_timeout):
        message = request(
            messages,
            self.settings.transport,
            self.tools,
            response_timeout=response_timeout,
        )
        calls = message.get("tool_calls")
        if not isinstance(calls, list) or len(calls) != 1:
            raise ProtocolError("Expected exactly one structured tool call")
        try:
            call = calls[0]
            if call["type"] != "function":
                raise KeyError
            function = call["function"]
            return {"tool": function["name"], "arguments": object_json(function["arguments"])}
        except (KeyError, TypeError):
            raise ProtocolError("Malformed tool call") from None

    def run(self, task, on_progress=None, on_message=None, attempt=1, timeout=None):
        timeout = self.settings.attempt_timeout if timeout is None else timeout
        deadline = time.monotonic() + max(0, timeout)
        state = self.state_class(self.settings, deadline)
        if not isinstance(task, str) or not task.strip() or len(task) > self.settings.task_limit:
            return state.result("failed", f"Task must contain 1–{self.settings.task_limit} characters")
        try:
            state.prepare()
        except (OSError, ProtocolError, ValueError) as exc:
            return state.result("failed", f"Tool environment unavailable: {clip(exc, 300)}")
        feedback = ""
        protocol_errors = 0
        for step_number in range(1, self.settings.step_limit + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return state.result("failed", f"{self.settings.name} timed out after {timeout:g} seconds", True)
            snapshot = {
                "files": state.files[-6:],
                "pending": {key: len(value) for key, value in list(state.pending.items())[-4:]},
                "unresolved": [clip(value, 120) for value in sorted(state.failures)[-6:]],
                "recent_results": state.receipts[-2:],
                "feedback": feedback,
            }
            while len(json.dumps(snapshot, ensure_ascii=False)) > self.settings.snapshot_limit and snapshot["recent_results"]:
                snapshot["recent_results"].pop(0)
            messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": clip(task, self.settings.task_limit)},
                {"role": "user", "content": json.dumps(snapshot, ensure_ascii=False)},
            ]
            if on_progress:
                on_progress(f"{self.settings.name} обдумывает следующий шаг")
            emit_message(
                on_message,
                attempt=attempt,
                step=step_number,
                sender=self.settings.actor,
                recipient="llm",
                kind="request",
                content=messages[1:],
            )
            response_started = time.monotonic()

            def log_llm_response(content):
                emit_message(
                    on_message,
                    attempt=attempt,
                    step=step_number,
                    sender="llm",
                    recipient=self.settings.actor,
                    kind="response",
                    content=content,
                    response_seconds=round(time.monotonic() - response_started, 3),
                )

            try:
                data = self.ask_llm(messages, min(self.settings.transport.response_timeout, remaining))
            except TransportTimeout as exc:
                log_llm_response({"status": "timeout", "error": str(exc)})
                return state.result("failed", f"{self.settings.name} did not receive an LLM response in time", True)
            except TransportError as exc:
                log_llm_response({"status": "transport_error", "error": str(exc)})
                return state.result("failed", str(exc))
            except ProtocolError as exc:
                log_llm_response({"status": "protocol_error", "error": str(exc)})
                feedback = str(exc)
                protocol_errors += 1
                if protocol_errors >= 3:
                    return state.result("failed", feedback)
                continue
            log_llm_response(data)
            try:
                if data.get("tool") == "finish":
                    arguments = data["arguments"]
                    status = arguments.get("status")
                    if status not in {"success", "failed"}:
                        raise ProtocolError("Invalid status")
                    return state.result(status, string(arguments, "summary"))
            except ProtocolError as exc:
                feedback = str(exc)
                protocol_errors += 1
                if protocol_errors >= 3:
                    return state.result("failed", feedback)
                continue
            protocol_errors = 0
            if time.monotonic() >= deadline:
                return state.result("failed", f"{self.settings.name} timed out after {timeout:g} seconds", True)
            arguments = data.get("arguments", {})
            identity = str(data.get("tool")) + ":" + clip(
                arguments.get("command", arguments.get("path", "")), 600
            )
            try:
                if on_progress:
                    on_progress(f"{self.settings.name}: {data.get('tool')}")
                tool_result = state.execute(data)
            except (ProtocolError, OSError, ValueError) as exc:
                tool_result = {"success": False, "error": clip(exc, 200)}
            if tool_result["success"]:
                state.failures.discard(identity)
            else:
                state.failures.add(identity)
            state.receipts.append({"tool": data.get("tool"), "target": identity, "result": tool_result})
            if time.monotonic() >= deadline:
                return state.result("failed", f"{self.settings.name} timed out after {timeout:g} seconds", True)
            feedback = ""
        return state.result("failed", f"{self.settings.name} step limit exceeded")
