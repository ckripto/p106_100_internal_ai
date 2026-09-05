"""Developer entry point."""

from agents.shared import ToolAgent

from .settings import SETTINGS

AGENT = ToolAgent(SETTINGS)


def run_agent(task, on_progress=None, on_message=None, attempt=1, timeout=None):
    return AGENT.run(
        task, on_progress=on_progress, on_message=on_message,
        attempt=attempt, timeout=timeout,
    )


def main():
    import json

    print(json.dumps(run_agent(input("Задача для Developer: ")), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
