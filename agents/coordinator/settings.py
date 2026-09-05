"""Coordinator-specific configuration."""

import os
from dataclasses import dataclass
from pathlib import Path

from agents.shared import TransportSettings


@dataclass(frozen=True)
class CoordinatorSettings:
    prompt_path: Path
    transport: TransportSettings
    max_delegations: int
    max_timeouts: int
    max_protocol_errors: int
    task_limit: int = 1800
    history_pairs: int = 2


SETTINGS = CoordinatorSettings(
    prompt_path=Path(__file__).with_name("prompt.md"),
    transport=TransportSettings(
        url=os.environ.get("COORDINATOR_LLM_URL", "http://localhost:8080/v1/chat/completions"),
        response_timeout=float(os.environ.get("COORDINATOR_LLM_RESPONSE_TIMEOUT", "300")),
    ),
    max_delegations=int(os.environ.get("COORDINATOR_MAX_DELEGATIONS", "8")),
    max_timeouts=int(os.environ.get("COORDINATOR_MAX_TIMEOUTS", "3")),
    max_protocol_errors=int(os.environ.get("COORDINATOR_MAX_PROTOCOL_ERRORS", "3")),
)
