"""Adapter catalog: adapter_type -> class. The runtime picks agents by declared
capabilities, never by adapter type or provider (BR-03)."""

from typing import Any

from office_contracts import ConfigField

from agent_adapters.base import AgentAdapter
from agent_adapters.claude_cli import ClaudeCliAdapter
from agent_adapters.codex_cli import CodexCliAdapter
from agent_adapters.mock import MockAdapter
from agent_adapters.mock_jobs import MockModelJobAdapter, MockMusicJobAdapter

ADAPTERS: dict[str, type] = {
    MockAdapter.adapter_type: MockAdapter,
    ClaudeCliAdapter.adapter_type: ClaudeCliAdapter,
    CodexCliAdapter.adapter_type: CodexCliAdapter,
    MockMusicJobAdapter.adapter_type: MockMusicJobAdapter,
    MockModelJobAdapter.adapter_type: MockModelJobAdapter,
}


def config_fields(adapter_type: str) -> list[ConfigField]:
    return list(getattr(ADAPTERS[adapter_type], "config_fields", []))


def create_adapter(adapter_type: str, config: dict[str, Any] | None = None) -> AgentAdapter:
    """Raises ValueError for an unknown type or invalid config."""
    try:
        cls = ADAPTERS[adapter_type]
    except KeyError:
        raise ValueError(f"unknown adapter_type {adapter_type!r}") from None
    adapter = cls(config or {})
    adapter.describe_capabilities()  # validates capability config
    return adapter
