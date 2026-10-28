"""Claude via the Claude Agent SDK (runs the locally logged-in Claude Code CLI).

Config:
  model: str                     e.g. "claude-sonnet-5-5"; default: CLI default
  tools: list[str]               built-in tools available; default for workers
                                 ["Read", "Write", "Edit", "Glob", "Grep"]; [] = none.
                                 Invocations without a workdir always get no tools.
  permission_mode: str           default "acceptEdits" (edits only inside workdir)
  max_turns: int                 default 30
  effort: str                    low | medium | high | xhigh | max
  capabilities: list[str]        default ["chat", "code"]
  system_prompt: str             appended role description
"""

import tempfile
from typing import Any

from office_contracts import AgentEventKind, AgentResult, CapabilityManifest

from agent_adapters.runner import AdapterRunError, RunContext, TaskBackedAdapter

DEFAULT_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]


class ClaudeAgentSdkAdapter(TaskBackedAdapter):
    adapter_type = "claude-agent-sdk"

    def __init__(self, config=None, query_fn=None):
        super().__init__(config)
        self._query_fn = query_fn  # injected in tests

    def describe_capabilities(self) -> CapabilityManifest:
        return CapabilityManifest(
            adapter_type=self.adapter_type,
            provider="anthropic",
            capabilities=self.config.get("capabilities", ["chat", "code"]),
            invocation_modes=["stream"],
            required_permissions=["fs.workdir.write"],
            supported_artifact_types=["code", "document"],
            cost_model={"unit": "token", "notes": "reported per run by the SDK (total_cost_usd)"},
            observability_level="transcript",
            supports_cancel=True,
        )

    def build_options(self, ctx: RunContext) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        inv = ctx.invocation
        # No workdir (e.g. an Orchestrator turn) means no filesystem access at all.
        tools = self.config.get("tools", DEFAULT_TOOLS) if inv.workdir else []
        options: dict[str, Any] = {
            "cwd": inv.workdir or tempfile.gettempdir(),
            "add_dirs": inv.read_dirs,
            "tools": tools,
            "allowed_tools": tools,
            "permission_mode": self.config.get("permission_mode", "acceptEdits"),
            "max_turns": self.config.get("max_turns", 30),
            "setting_sources": [],  # do not inherit the user's Claude Code settings/hooks
        }
        if self.config.get("system_prompt"):
            options["system_prompt"] = {
                "type": "preset",
                "preset": "claude_code",
                "append": self.config["system_prompt"],
            }
        if self.config.get("model"):
            options["model"] = self.config["model"]
        if self.config.get("effort"):
            options["effort"] = self.config["effort"]
        if inv.limits.max_cost is not None:
            options["max_budget_usd"] = inv.limits.max_cost
        if inv.output_schema is not None:
            options["output_format"] = {"type": "json_schema", "schema": inv.output_schema}
        return ClaudeAgentOptions(**options)

    async def _execute(self, ctx: RunContext) -> AgentResult:
        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            TextBlock,
            ToolUseBlock,
            query,
        )

        query_fn = self._query_fn or query
        result: ResultMessage | None = None
        async for message in query_fn(
            prompt=ctx.invocation.instructions, options=self.build_options(ctx)
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        await ctx.emit(AgentEventKind.MESSAGE, text=block.text)
                    elif isinstance(block, ToolUseBlock):
                        await ctx.emit(AgentEventKind.TOOL_CALL, name=block.name, input=block.input)
            elif isinstance(message, ResultMessage):
                result = message
                ctx.provider_ref = message.session_id

        if result is None:
            raise AdapterRunError("no_result", "SDK stream ended without a result", retryable=True)
        if result.is_error:
            raise AdapterRunError(
                result.subtype or "claude_error",
                str(result.errors or result.result or result.subtype),
                retryable=result.subtype not in {"error_max_budget_usd"},
            )
        structured = result.structured_output
        if ctx.invocation.output_schema is not None and not isinstance(structured, dict):
            raise AdapterRunError("bad_structured_output", "missing structured output", True)
        return AgentResult(
            text=result.result or "",
            structured=structured if isinstance(structured, dict) else None,
            cost_usd=result.total_cost_usd,
        )
