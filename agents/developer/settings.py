"""Developer-specific configuration."""

import os
from pathlib import Path

from agents.shared import AgentSettings, TransportSettings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOCAL_INSTRUCTIONS = PROJECT_ROOT / "AGENTS.local.md"

SETTINGS = AgentSettings(
    actor="developer",
    name="Developer",
    root=PROJECT_ROOT,
    prompt_path=Path(__file__).with_name("prompt.md"),
    transport=TransportSettings(
        url=os.environ.get("DEVELOPER_LLM_URL", "http://localhost:8080/v1/chat/completions"),
        response_timeout=float(os.environ.get("DEVELOPER_LLM_RESPONSE_TIMEOUT", "300")),
        max_tokens=int(os.environ.get("DEVELOPER_MAX_TOKENS", "700")),
    ),
    attempt_timeout=float(os.environ.get("DEVELOPER_TIMEOUT", "300")),
    command_timeout=float(os.environ.get("DEVELOPER_COMMAND_TIMEOUT", "120")),
    step_limit=int(os.environ.get("DEVELOPER_STEP_LIMIT", "100")),
    chunk_size=int(os.environ.get("DEVELOPER_CHUNK_SIZE", "1000")),
    staging_limit=256_000,
    task_limit=1200,
    snapshot_limit=1800,
    instruction_paths=tuple(path for path in (
        PROJECT_ROOT / "AGENTS.md",
        LOCAL_INSTRUCTIONS,
        Path(__file__).with_name("AGENTS.md"),
    ) if path.exists()),
    excluded_names=frozenset({".git", ".pytest_cache", "__pycache__", "data", "venv"}),
)
