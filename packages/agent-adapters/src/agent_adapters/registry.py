"""Adapter catalog: adapter_type -> class. The runtime picks agents by declared
capabilities, never by adapter type or provider (BR-03)."""

from typing import Any

from agent_adapters.base import AgentAdapter
from agent_adapters.claude_sdk import ClaudeAgentSdkAdapter
from agent_adapters.codex_cli import CodexCliAdapter
from agent_adapters.mock import MockAdapter

ADAPTERS: dict[str, type] = {
    MockAdapter.adapter_type: MockAdapter,
    ClaudeAgentSdkAdapter.adapter_type: ClaudeAgentSdkAdapter,
    CodexCliAdapter.adapter_type: CodexCliAdapter,
}


def create_adapter(adapter_type: str, config: dict[str, Any] | None = None) -> AgentAdapter:
    """Raises ValueError for an unknown type or invalid config."""
    try:
        cls = ADAPTERS[adapter_type]
    except KeyError:
        raise ValueError(f"unknown adapter_type {adapter_type!r}") from None
    adapter = cls(config or {})
    adapter.describe_capabilities()  # validates capability config
    return adapter
