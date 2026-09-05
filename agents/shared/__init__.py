"""Shared transport and tool runtime for local agents."""

from .events import emit_message
from .protocol import ProtocolError, TransportError, TransportSettings, TransportTimeout, clip
from .runtime import AgentSettings, ToolAgent, ToolState

__all__ = [
    "AgentSettings", "ProtocolError", "ToolAgent", "ToolState",
    "TransportError", "TransportSettings", "TransportTimeout", "clip", "emit_message",
]
