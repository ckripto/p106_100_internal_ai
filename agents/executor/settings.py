"""Executor-specific configuration."""

import os
from pathlib import Path

from agents.shared import AgentSettings, TransportSettings

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SETTINGS = AgentSettings(
    actor="executor",
    name="Исполнитель",
    root=Path(os.environ.get("EXECUTOR_WORKSPACE", PROJECT_ROOT / "workspace")),
    prompt_path=Path(__file__).with_name("prompt.md"),
    transport=TransportSettings(
        url=os.environ.get("EXECUTOR_LLM_URL", "http://localhost:8080/v1/chat/completions"),
        response_timeout=float(os.environ.get("EXECUTOR_LLM_RESPONSE_TIMEOUT", "300")),
    ),
    attempt_timeout=float(os.environ.get("EXECUTOR_TIMEOUT", "300")),
    command_timeout=float(os.environ.get("EXECUTOR_COMMAND_TIMEOUT", "180")),
    step_limit=int(os.environ.get("EXECUTOR_STEP_LIMIT", "60")),
    chunk_size=int(os.environ.get("EXECUTOR_CHUNK_SIZE", "600")),
    redundant_path_prefix="workspace",
)
