"""Planning, routing and bounded recovery across local agents."""

import json
import time

from agents.developer import run_agent as run_developer
from agents.executor import run_agent as run_executor
from agents.shared import ProtocolError, TransportError, clip
from agents.shared.protocol import object_json, request, string

from .settings import SETTINGS


class Coordinator:
    def __init__(self, settings=SETTINGS, runners=None):
        self.settings = settings
        self.prompt = settings.prompt_path.read_text(encoding="utf-8").strip()
        self.runners = runners or {"executor": run_executor, "developer": run_developer}

    def ask_llm(self, messages):
        message = request(messages, self.settings.transport)
        return object_json(message.get("content"))

    def _compact_result(self, agent_name, delegated, result):
        if not isinstance(result, dict) or result.get("status") not in {"success", "failed"}:
            raise ProtocolError(f"{agent_name} returned an invalid result")
        return {
            "agent": agent_name,
            "task": clip(delegated, 300),
            "status": result["status"],
            "timed_out": bool(result.get("timed_out")),
            "summary": clip(result.get("summary", "No summary"), 300),
            "files": result.get("files", [])[-6:],
            "commands": [
                {
                    "command": clip(command.get("command", ""), 100),
                    "success": bool(command.get("success")),
                    "timed_out": bool(command.get("timed_out")),
                }
                for command in result.get("commands", [])[-3:]
            ],
        }

    def run(self, task, history=None, on_progress=None, on_message=None):
        history = history if history is not None else []
        if not isinstance(task, str) or not task.strip() or len(task) > self.settings.task_limit:
            return {
                "type": "final",
                "status": "failed",
                "summary": f"Задача должна содержать 1–{self.settings.task_limit} символов; разбейте её на уточнения",
            }
        history_messages = history[-self.settings.history_pairs * 2:]
        base = ([{"role": "system", "content": self.prompt}] + history_messages
                + [{"role": "user", "content": task}])
        attempts = []
        latest = None
        result = None
        recovery_target = None
        feedback = ""
        protocol_errors = 0
        max_decisions = self.settings.max_delegations * 2 + 4

        for _ in range(max_decisions):
            try:
                if on_progress:
                    action = "перепланирует задачу после тайм-аута" if recovery_target else "обдумывает следующий шаг"
                    on_progress("Координатор " + action)
                messages = list(base)
                if attempts or feedback:
                    coordination_state = {
                        "type": "coordination_state",
                        "attempts": attempts,
                        "instruction": "Выбери следующий небольшой шаг или дай итог.",
                    }
                    if feedback:
                        coordination_state["correction"] = feedback
                    messages.append({
                        "role": "user",
                        "content": json.dumps(coordination_state, ensure_ascii=False),
                    })
                decision = self.ask_llm(messages)
                feedback = ""

                if decision.get("type") == "final":
                    status = decision.get("status")
                    if status not in {"success", "failed"}:
                        raise ProtocolError("Invalid final status")
                    summary = string(decision, "summary")
                    if status == "success" and (latest is None or latest["status"] != "success"):
                        raise ProtocolError("Success requires a successful agent result for this task")
                    if status == "failed" and recovery_target and len(attempts) < self.settings.max_delegations:
                        raise ProtocolError("After a timeout, delegate a smaller recovery task before giving up")
                    result = {"type": "final", "status": status, "summary": summary}
                    break

                agent_name = decision.get("agent")
                if decision.get("type") != "delegate" or agent_name not in self.runners:
                    raise ProtocolError("Expected delegate to executor/developer or final")
                delegated = string(decision, "task", self.settings.task_limit)
                if len(attempts) >= self.settings.max_delegations:
                    result = {
                        "type": "final",
                        "status": "failed",
                        "summary": "Координатор исчерпал лимит делегаций",
                    }
                    break
                if recovery_target == (agent_name, delegated.strip().casefold()):
                    raise ProtocolError("Timed-out task must be narrowed or split, not repeated verbatim")

                recovery_target = None
                protocol_errors = 0
                attempt_number = len(attempts) + 1
                if on_progress:
                    on_progress(
                        f"{agent_name}: попытка {attempt_number} из {self.settings.max_delegations}"
                    )
                if on_message:
                    on_message({
                        "attempt": attempt_number,
                        "sender": "coordinator",
                        "recipient": agent_name,
                        "kind": "request",
                        "content": delegated,
                        "created": time.time(),
                        "response_seconds": None,
                    })
                response_started = time.monotonic()
                latest = self.runners[agent_name](delegated, on_progress=on_progress)
                compact = self._compact_result(agent_name, delegated, latest)
                if on_message:
                    on_message({
                        "attempt": attempt_number,
                        "sender": agent_name,
                        "recipient": "coordinator",
                        "kind": "response",
                        "content": json.dumps(compact, ensure_ascii=False),
                        "created": time.time(),
                        "response_seconds": round(time.monotonic() - response_started, 3),
                    })
                attempts.append(compact)
                if compact["timed_out"]:
                    recovery_target = (agent_name, delegated.strip().casefold())
                    if on_progress:
                        on_progress(f"{agent_name} не ответил вовремя; координатор упрощает задачу")
                if sum(item["timed_out"] for item in attempts) >= self.settings.max_timeouts:
                    result = {
                        "type": "final",
                        "status": "failed",
                        "summary": "Агенты несколько раз превысили время ожидания; выполнение остановлено",
                    }
                    break
            except ProtocolError as exc:
                feedback = clip(exc, 300)
                protocol_errors += 1
                if protocol_errors >= self.settings.max_protocol_errors:
                    detail = clip(latest["summary"], 300) if latest else "Агенты ещё не запускались"
                    result = {
                        "type": "final",
                        "status": "failed",
                        "summary": (
                            f"Координатор не смог сформировать допустимое решение: {feedback}. "
                            f"Последний результат: {detail}"
                        ),
                    }
                    break
            except TransportError as exc:
                result = {"type": "final", "status": "failed", "summary": str(exc)}
                break

        if result is None:
            result = {"type": "final", "status": "failed", "summary": "Coordinator decision limit exceeded"}
        history.extend([
            {"role": "user", "content": clip(task, 500)},
            {"role": "assistant", "content": json.dumps(result, ensure_ascii=False)},
        ])
        del history[:-self.settings.history_pairs * 2]
        return result


DEFAULT_COORDINATOR = Coordinator()


def run_coordinator(task, history=None, on_progress=None, on_message=None):
    return DEFAULT_COORDINATOR.run(
        task,
        history=history,
        on_progress=on_progress,
        on_message=on_message,
    )


def main():
    history = []
    print("Coordinator запущен. Для выхода введи exit.\n")
    while True:
        try:
            task = input("Задача: ").strip()
            if task.lower() in {"exit", "quit", "выход"}:
                break
            if task:
                print(json.dumps(run_coordinator(task, history), ensure_ascii=False, indent=2))
        except (KeyboardInterrupt, EOFError):
            print("\nВыход.")
            break


if __name__ == "__main__":
    main()
