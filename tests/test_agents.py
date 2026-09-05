import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from agents.coordinator.agent import Coordinator
from agents.coordinator.settings import SETTINGS as COORDINATOR_SETTINGS
from agents.developer.agent import AGENT as DEVELOPER_AGENT
from agents.developer.settings import SETTINGS as DEVELOPER_SETTINGS
from agents.executor.agent import AGENT as EXECUTOR_AGENT
from agents.executor.runtime import FILE_SCRIPT, LIST_SCRIPT, PREPARE_SCRIPT, SSHToolState
from agents.executor.settings import SETTINGS as EXECUTOR_SETTINGS
from agents.shared import ProtocolError, ToolAgent, ToolState, TransportTimeout
from agents.shared import protocol


@pytest.fixture
def tool_agent(tmp_path):
    settings = replace(EXECUTOR_SETTINGS, root=tmp_path)
    return ToolAgent(settings)


def reply(monkeypatch, choice):
    response = Mock()
    response.json.return_value = {"choices": [choice]}
    monkeypatch.setattr(protocol.requests, "post", Mock(return_value=response))


def tool_call(name, **arguments):
    return {"tool": name, "arguments": arguments}


@pytest.mark.parametrize("reason", ["length", None, "content_filter"])
def test_incomplete_never_executes(monkeypatch, tool_agent, reason):
    reply(monkeypatch, {
        "finish_reason": reason,
        "message": {"content": "SECRET broken source"},
    })
    result = tool_agent.run("create file")
    assert result["status"] == "failed"
    assert "SECRET" not in json.dumps(result)
    assert not list(tool_agent.settings.root.iterdir())


@pytest.mark.parametrize("payload", ["[]", "null", "1", "{broken"])
def test_invalid_objects(payload):
    with pytest.raises(ProtocolError):
        protocol.object_json(payload)


def test_transport(monkeypatch, tool_agent):
    monkeypatch.setattr(
        protocol.requests,
        "post",
        Mock(side_effect=requests.ConnectionError("secret")),
    )
    assert tool_agent.run("x")["status"] == "failed"
    coordinator = Coordinator(COORDINATOR_SETTINGS, {"executor": Mock()})
    assert coordinator.run("x")["status"] == "failed"


def test_executor_marks_llm_timeout(monkeypatch, tool_agent):
    monkeypatch.setattr(
        tool_agent,
        "ask_llm",
        Mock(side_effect=TransportTimeout("late")),
    )
    messages = []
    result = tool_agent.run("x", on_message=messages.append, attempt=2)
    assert result["status"] == "failed"
    assert result["timed_out"] is True
    assert [(item["sender"], item["recipient"]) for item in messages] == [
        ("executor", "llm"), ("llm", "executor")
    ]
    assert messages[0]["step"] == messages[1]["step"] == 1
    assert json.loads(messages[1]["content"])["status"] == "timeout"
    assert messages[1]["response_seconds"] >= 0


def test_coordinator_logs_llm_timeout(monkeypatch):
    coordinator = Coordinator(COORDINATOR_SETTINGS, {"executor": Mock()})
    monkeypatch.setattr(coordinator, "ask_llm", Mock(side_effect=TransportTimeout("late")))
    messages = []
    assert coordinator.run("x", on_message=messages.append)["status"] == "failed"
    assert [(item["sender"], item["recipient"]) for item in messages] == [
        ("coordinator", "llm"), ("llm", "coordinator")
    ]
    assert json.loads(messages[1]["content"])["status"] == "timeout"


def test_atomic_chunks(tool_agent):
    state = ToolState(tool_agent.settings)
    target = tool_agent.settings.root / "a.py"
    target.write_text("old")

    state.execute(tool_call("write_file", path="a.py", content="hello", offset=0, final=False))
    assert target.read_text() == "old"
    with pytest.raises(ProtocolError):
        state.execute(tool_call("write_file", path="a.py", content="bad", offset=0, final=True))
    with pytest.raises(ProtocolError):
        state.execute(tool_call(
            "write_file",
            path="a.py",
            content="x" * (tool_agent.settings.chunk_size + 1),
            offset=5,
            final=True,
        ))
    assert state.result("success", "done")["status"] == "failed"
    state.execute(tool_call("write_file", path="a.py", content=" world", offset=5, final=True))
    assert target.read_text() == "hello world"
    assert state.result("success", "done")["status"] == "success"


def test_exact_edit_is_atomic(tool_agent):
    state = ToolState(tool_agent.settings)
    target = tool_agent.settings.root / "theme.css"
    target.write_text("green" + "x" * 7000 + "green")
    state.execute(tool_call("read_file", path="theme.css", offset=0))

    result = state.execute(tool_call(
        "edit_file",
        path="theme.css",
        old="green",
        new="blue",
        expected_replacements=2,
    ))

    assert result["replacements"] == 2
    assert target.read_text() == "blue" + "x" * 7000 + "blue"
    with pytest.raises(ProtocolError, match="Expected 3 exact replacement"):
        state.execute(tool_call(
            "edit_file",
            path="theme.css",
            old="blue",
            new="red",
            expected_replacements=3,
        ))
    assert target.read_text() == "blue" + "x" * 7000 + "blue"


def test_developer_requires_complete_nearest_instructions(tmp_path):
    settings = replace(
        DEVELOPER_SETTINGS,
        root=tmp_path,
        instruction_paths=(),
        chunk_size=10,
        enforce_instruction_reads=True,
    )
    state = ToolState(settings)
    module = tmp_path / "module"
    module.mkdir()
    (module / "AGENTS.md").write_text("0123456789rules")
    target = module / "code.py"
    target.write_text("old")

    with pytest.raises(ProtocolError, match="module/AGENTS.md"):
        state.execute(tool_call(
            "edit_file", path="module/code.py", old="old", new="new",
            expected_replacements=1,
        ))
    state.execute(tool_call("read_file", path="module/AGENTS.md", offset=0))
    with pytest.raises(ProtocolError, match="complete nearest instruction"):
        state.execute(tool_call(
            "edit_file", path="module/code.py", old="old", new="new",
            expected_replacements=1,
        ))
    state.execute(tool_call("read_file", path="module/AGENTS.md", offset=10))
    with pytest.raises(ProtocolError, match="Read module/code.py"):
        state.execute(tool_call(
            "edit_file", path="module/code.py", old="old", new="new",
            expected_replacements=1,
        ))
    state.execute(tool_call("read_file", path="module/code.py", offset=0))
    assert state.execute(tool_call(
        "edit_file", path="module/code.py", old="old", new="new",
        expected_replacements=1,
    ))["success"]
    assert target.read_text() == "new"


def test_paths(tool_agent):
    (tool_agent.settings.root / "link").symlink_to("/tmp")
    state = ToolState(tool_agent.settings)
    for path in ["../x", "/tmp/x", "link/x"]:
        with pytest.raises(ProtocolError):
            state.safe_path(path)
    with pytest.raises(ProtocolError, match="remove the workspace/ prefix"):
        state.safe_path("workspace/a.py")


def test_executor_uses_remote_ssh_state():
    assert EXECUTOR_AGENT.state_class is SSHToolState


def test_ssh_state_routes_tools_to_remote(monkeypatch):
    settings = replace(
        EXECUTOR_SETTINGS,
        root=Path("/root/agents-workspace"),
        ssh_target="root@executor.example",
    )
    state = SSHToolState(settings)
    remote_python = Mock(
        side_effect=["", "", "abc", "whole", '{"success":true,"files":[],"truncated":false}']
    )
    monkeypatch.setattr(state, "_remote_python", remote_python)
    state.prepare()
    state._write_file("a.txt", "abc")
    assert state._read_file("a.txt", 0) == "abc"
    assert state._read_all("a.txt") == "whole"
    assert state._list_files()["files"] == []
    assert [call.args[0] for call in remote_python.call_args_list] == [
        PREPARE_SCRIPT, FILE_SCRIPT, FILE_SCRIPT, FILE_SCRIPT, LIST_SCRIPT,
    ]
    assert remote_python.call_args_list[1].kwargs["input_bytes"] == b"abc"

    run_process = Mock(return_value={"success": True, "returncode": 0, "timed_out": False})
    monkeypatch.setattr(state, "_run_process", run_process)
    assert state._run_command("pwd")["success"]
    arguments, cwd = run_process.call_args.args
    assert arguments[0] == "ssh" and "root@executor.example" in arguments
    assert "/root/agents-workspace" in arguments[-1] and "pwd" in arguments[-1]
    assert cwd is None


def test_remote_file_scripts_are_atomic_and_bounded(tmp_path):
    root = tmp_path / "remote"
    subprocess.run([sys.executable, "-c", PREPARE_SCRIPT, str(root)], check=True)
    subprocess.run(
        [sys.executable, "-c", FILE_SCRIPT, "write", str(root), "a.txt"],
        input="привет".encode(),
        check=True,
    )
    read = subprocess.run(
        [sys.executable, "-c", FILE_SCRIPT, "read", str(root), "a.txt", "0", "20"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert read.stdout == "привет"
    listed = subprocess.run(
        [sys.executable, "-c", LIST_SCRIPT, str(root), "[]"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(listed.stdout) == {
        "success": True,
        "files": ["a.txt"],
        "truncated": False,
    }
    escaped = subprocess.run(
        [sys.executable, "-c", FILE_SCRIPT, "read", str(root), "../outside", "0", "20"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert escaped.returncode != 0


def test_commands(tool_agent):
    state = ToolState(tool_agent.settings)
    result = state.execute(tool_call(
        "run_command",
        command="python3 -c 'print(\"x\" * 100000)'",
    ))
    assert result["success"] and result["output_truncated"]
    assert len(result["stdout"]) < 650
    assert not state.execute(tool_call("run_command", command="exit 7"))["success"]
    assert not state.execute(tool_call("run_command", command="false | tail -1"))["success"]

    fast_settings = replace(tool_agent.settings, command_timeout=0.1)
    fast_state = ToolState(fast_settings)
    assert fast_state.execute(tool_call("run_command", command="sleep 10"))["timed_out"]


def test_false_success(monkeypatch, tool_agent):
    monkeypatch.setattr(tool_agent, "ask_llm", lambda messages, timeout: tool_call(
        "finish", status="success", summary="done"
    ))
    assert tool_agent.run("run a test")["status"] == "failed"

    coordinator = Coordinator(COORDINATOR_SETTINGS, {"executor": Mock()})
    monkeypatch.setattr(coordinator, "ask_llm", lambda messages: {
        "type": "final", "status": "success", "summary": "done"
    })
    assert coordinator.run("run test")["status"] == "failed"


def test_coordinator_answers_informational_question(monkeypatch):
    runner = Mock()
    coordinator = Coordinator(COORDINATOR_SETTINGS, {"executor": runner})
    monkeypatch.setattr(coordinator, "ask_llm", lambda messages: {
        "type": "answer",
        "summary": "Столица Франции — Париж.",
        "reason": "Инструменты не требуются.",
    })
    result = coordinator.run("Какая столица у Франции?")
    assert result == {
        "type": "final",
        "status": "success",
        "summary": "Столица Франции — Париж.",
    }
    runner.assert_not_called()


def test_coordinator_routes_fresh_lookup_without_planning_round(monkeypatch):
    runner = Mock(return_value={
        "status": "success",
        "summary": "Актуальный курс биткоина: 79695 USD",
        "files": [],
        "commands": [{"command": "fetch price", "success": True}],
    })
    coordinator = Coordinator(COORDINATOR_SETTINGS, {"executor": runner})
    ask_llm = Mock()
    monkeypatch.setattr(coordinator, "ask_llm", ask_llm)
    messages = []

    result = coordinator.run("Какой сейчас курс биткойна?", on_message=messages.append)

    assert result == {
        "type": "final",
        "status": "success",
        "summary": "Актуальный курс биткоина: 79695 USD",
    }
    ask_llm.assert_not_called()
    delegated = runner.call_args.args[0]
    assert "Не создавай файл" in delegated
    assert "Не повторяй успешный запрос" in delegated
    assert [(item["sender"], item["recipient"]) for item in messages] == [
        ("coordinator", "executor"),
        ("executor", "coordinator"),
    ]


def test_coordinator_rejects_answer_after_delegation(monkeypatch):
    decisions = iter([
        {"type": "delegate", "agent": "executor", "task": "inspect"},
        {"type": "answer", "summary": "done", "reason": "no tools"},
        {"type": "final", "status": "success", "summary": "done"},
    ])
    runner = Mock(return_value={
        "status": "success", "summary": "inspected", "files": [], "commands": []
    })
    coordinator = Coordinator(COORDINATOR_SETTINGS, {"executor": runner})
    monkeypatch.setattr(coordinator, "ask_llm", lambda messages: next(decisions))
    assert coordinator.run("inspect") == {
        "type": "final", "status": "success", "summary": "done"
    }


def test_coordinator_bounds_protocol_corrections(monkeypatch):
    coordinator = Coordinator(COORDINATOR_SETTINGS, {"executor": Mock()})
    ask = Mock(return_value={"type": "unexpected"})
    monkeypatch.setattr(coordinator, "ask_llm", ask)
    result = coordinator.run("run test")
    assert result["status"] == "failed"
    assert ask.call_count == coordinator.settings.max_protocol_errors
    assert "Expected answer" in result["summary"]


def test_failed_command_cannot_be_hidden(monkeypatch, tool_agent):
    calls = iter([
        tool_call("run_command", command="exit 1"),
        tool_call("run_command", command="true"),
        tool_call("finish", status="success", summary="done"),
    ])
    monkeypatch.setattr(tool_agent, "ask_llm", lambda messages, timeout: next(calls))
    assert tool_agent.run("test")["status"] == "failed"


def test_recovery_and_no_payload_in_context(monkeypatch, tool_agent):
    calls = iter([
        ProtocolError("Incomplete output"),
        tool_call("write_file", path="ok.py", content="print(42)", offset=0, final=True),
        tool_call("run_command", command="python3 ok.py"),
        tool_call("finish", status="success", summary="done"),
    ])
    seen = []

    def ask(messages, timeout):
        seen.append(json.dumps(messages))
        data = next(calls)
        if isinstance(data, Exception):
            raise data
        return data

    monkeypatch.setattr(tool_agent, "ask_llm", ask)
    result = tool_agent.run("write and run")
    assert result["status"] == "success"
    assert result["commands"][0]["stdout"] == "42\n"
    assert "print(42)" not in seen[-1]


def test_tool_agent_logs_each_llm_step(monkeypatch, tool_agent):
    calls = iter([
        tool_call("list_files", reason="inspect workspace"),
        tool_call("finish", status="success", summary="listed", reason="inspection complete"),
    ])
    monkeypatch.setattr(tool_agent, "ask_llm", lambda messages, timeout: next(calls))
    messages = []
    assert tool_agent.run("list files", on_message=messages.append, attempt=3)["status"] == "success"
    assert [(item["sender"], item["recipient"], item["step"]) for item in messages] == [
        ("executor", "llm", 1), ("llm", "executor", 1),
        ("executor", "llm", 2), ("llm", "executor", 2),
    ]
    assert "Ты Executor" not in messages[0]["content"]
    assert json.loads(messages[1]["content"])["arguments"]["reason"] == "inspect workspace"


def test_tool_agent_stops_repeated_observation_loop(monkeypatch, tool_agent):
    agent = ToolAgent(replace(tool_agent.settings, snapshot_limit=1800))
    (tool_agent.settings.root / "large.txt").write_text("x" * 3000)
    calls = iter([
        tool_call("read_file", path="large.txt", offset=0),
        tool_call("read_file", path="large.txt", offset=1000),
        tool_call("read_file", path="large.txt", offset=0),
        tool_call("read_file", path="large.txt", offset=1000),
    ])
    snapshots = []

    def ask(messages, timeout):
        snapshots.append(json.loads(messages[-1]["content"]))
        return next(calls)

    monkeypatch.setattr(agent, "ask_llm", ask)
    result = agent.run("inspect large file")

    assert result["status"] == "failed"
    assert result["timed_out"] is False
    assert "Repeated tool loop" in result["summary"]
    assert len(snapshots) == 4
    assert [item["target"] for item in snapshots[2]["recent_actions"]] == [
        "read_file:large.txt@0", "read_file:large.txt@1000",
    ]


def test_successful_external_request_prompts_immediate_finish(monkeypatch, tool_agent):
    calls = iter([
        tool_call(
            "run_command",
            command="printf 79695 # https://api.example/price",
            reason="fetch current price",
        ),
        tool_call(
            "finish",
            status="success",
            summary="BTC is 79695 USD",
            reason="current value received",
        ),
    ])
    snapshots = []

    def ask(messages, timeout):
        snapshots.append(json.loads(messages[-1]["content"]))
        return next(calls)

    monkeypatch.setattr(tool_agent, "ask_llm", ask)
    result = tool_agent.run("fetch current BTC price")

    assert result["status"] == "success"
    assert "do not request the same volatile data again" in snapshots[1]["feedback"]
    assert "call finish now" in snapshots[1]["feedback"]


def test_session_and_boundary(monkeypatch):
    decisions = iter([
        {"type": "delegate", "agent": "executor", "task": "write a.py"},
        {"type": "final", "status": "success", "summary": "a.py created"},
    ])
    seen = []

    def ask(messages):
        seen.append(json.dumps(messages))
        return next(decisions)

    runner = Mock(return_value={
        "status": "success",
        "summary": "done",
        "files": ["a.py"],
        "commands": [{
            "command": "python3 a.py",
            "stdout": "SECRET SOURCE",
            "success": True,
            "returncode": 0,
            "timed_out": False,
        }],
    })
    coordinator = Coordinator(COORDINATOR_SETTINGS, {"executor": runner})
    monkeypatch.setattr(coordinator, "ask_llm", ask)
    history = []
    agent_messages = []
    assert coordinator.run("create", history, on_message=agent_messages.append)["status"] == "success"
    assert "SECRET" not in seen[-1]
    assert [(message["sender"], message["recipient"]) for message in agent_messages] == [
        ("coordinator", "llm"), ("llm", "coordinator"),
        ("coordinator", "executor"), ("executor", "coordinator"),
        ("coordinator", "llm"), ("llm", "coordinator"),
    ]
    delegation = agent_messages[2]
    agent_response = agent_messages[3]
    assert delegation["content"] == "write a.py"
    assert agent_response["response_seconds"] >= 0
    assert "SECRET" not in agent_response["content"]
    assert agent_messages[0]["step"] == 1
    assert json.loads(agent_messages[1]["content"])["type"] == "delegate"
    assert len(history) == 2

    monkeypatch.setattr(coordinator, "ask_llm", lambda messages: (
        seen.append(json.dumps(messages)) or
        {"type": "final", "status": "failed", "summary": "no"}
    ))
    coordinator.run("modify it", history)
    assert "a.py created" in seen[-1]


def test_coordinator_routes_developer(monkeypatch):
    decisions = iter([
        {"type": "delegate", "agent": "developer", "task": "update service tests"},
        {"type": "final", "status": "success", "summary": "updated"},
    ])
    developer = Mock(return_value={
        "status": "success", "summary": "done", "files": ["tests/test_agents.py"], "commands": []
    })
    executor = Mock()
    coordinator = Coordinator(
        COORDINATOR_SETTINGS,
        {"executor": executor, "developer": developer},
    )
    monkeypatch.setattr(coordinator, "ask_llm", lambda messages: next(decisions))
    assert coordinator.run("Доработай текущую агентскую систему")["status"] == "success"
    developer.assert_called_once()
    executor.assert_not_called()


def test_native_tool_call(monkeypatch, tool_agent):
    reply(monkeypatch, {
        "finish_reason": "tool_calls",
        "message": {"tool_calls": [{
            "type": "function",
            "function": {"name": "list_files", "arguments": "{}"},
        }]},
    })
    assert tool_agent.ask_llm([], 5) == tool_call("list_files")


def test_read_staged_and_published(tool_agent):
    state = ToolState(tool_agent.settings)
    state.execute(tool_call("write_file", path="a", content="abc", offset=0, final=False))
    read = state.execute(tool_call("read_file", path="a", offset=1))
    assert read["content"] == "bc" and read["eof"]
    assert not (tool_agent.settings.root / "a").exists()


@pytest.mark.parametrize("choice", [{}, {"finish_reason": "stop", "message": None}])
def test_malformed_api(monkeypatch, choice):
    reply(monkeypatch, choice)
    with pytest.raises(ProtocolError):
        protocol.request([], EXECUTOR_SETTINGS.transport)


def test_read_only_evidence(monkeypatch, tool_agent):
    calls = iter([
        tool_call("list_files"),
        tool_call("finish", status="success", summary="listed"),
    ])
    monkeypatch.setattr(tool_agent, "ask_llm", lambda messages, timeout: next(calls))
    assert tool_agent.run("list files")["status"] == "success"


def test_large_source_across_chunks(monkeypatch, tool_agent):
    source = "# Большой файл\n" + "".join(f"x{i} = {i}\n" for i in range(400)) + "print(x399)\n"
    calls = []
    chunk_size = tool_agent.settings.chunk_size
    for offset in range(0, len(source), chunk_size):
        chunk = source[offset:offset + chunk_size]
        calls.append(tool_call(
            "write_file",
            path="large.py",
            content=chunk,
            offset=offset,
            final=offset + len(chunk) == len(source),
        ))
    calls.extend([
        tool_call("run_command", command="python3 large.py"),
        tool_call("finish", status="success", summary="verified"),
    ])
    iterator = iter(calls)
    monkeypatch.setattr(tool_agent, "ask_llm", lambda messages, timeout: next(iterator))
    result = tool_agent.run("create large file")
    assert result["status"] == "success"
    assert (tool_agent.settings.root / "large.py").read_text() == source
    assert result["commands"][0]["stdout"] == "399\n"


def test_coordinator_replans_timed_out_agent(monkeypatch):
    decisions = iter([
        {"type": "delegate", "agent": "executor", "task": "build everything"},
        {"type": "final", "status": "failed", "summary": "give up"},
        {"type": "delegate", "agent": "executor", "task": "inspect and implement one part"},
        {"type": "final", "status": "success", "summary": "completed"},
    ])
    runner = Mock(side_effect=[
        {"status": "failed", "summary": "late", "timed_out": True, "files": [], "commands": []},
        {"status": "success", "summary": "done", "files": ["a.py"], "commands": []},
    ])
    coordinator = Coordinator(COORDINATOR_SETTINGS, {"executor": runner})
    monkeypatch.setattr(coordinator, "ask_llm", lambda messages: next(decisions))
    assert coordinator.run("create app")["status"] == "success"
    assert [call.args[0] for call in runner.call_args_list] == [
        "build everything", "inspect and implement one part"
    ]


def test_coordinator_stops_after_repeated_timeouts(monkeypatch):
    coordinator = Coordinator(COORDINATOR_SETTINGS, {"executor": Mock(return_value={
        "status": "failed", "summary": "late", "timed_out": True, "files": [], "commands": []
    })})
    counter = 0

    def decide(messages):
        nonlocal counter
        counter += 1
        return {"type": "delegate", "agent": "executor", "task": f"small step {counter}"}

    monkeypatch.setattr(coordinator, "ask_llm", decide)
    assert coordinator.run("create app")["status"] == "failed"
    assert counter == coordinator.settings.max_timeouts


def test_developer_has_project_root_and_instruction_context():
    assert DEVELOPER_SETTINGS.root == DEVELOPER_SETTINGS.root.resolve()
    assert DEVELOPER_SETTINGS.root == Path(__file__).resolve().parents[1]
    assert "Доступ и эксплуатационные права" in DEVELOPER_AGENT.system_prompt
    assert "Developer дорабатывает проект" in DEVELOPER_AGENT.system_prompt
