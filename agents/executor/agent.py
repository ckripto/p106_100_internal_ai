"""Executor entry point."""

from agents.shared import ToolAgent

from .settings import SETTINGS

AGENT = ToolAgent(SETTINGS)


def run_agent(task, on_progress=None, timeout=None):
    return AGENT.run(task, on_progress=on_progress, timeout=timeout)


def main():
    import json

    print(json.dumps(run_agent(input("Задача: ")), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
