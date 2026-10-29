"""Claude Code CLI (`claude`, logged in on this machine), driven through the
Claude Agent SDK Python library, which starts the CLI as a subprocess.

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
  cli_path: str                  Claude Code CLI; default: `claude` on PATH, else the
                                 copy bundled with the SDK

With ``invocation.sandbox.mode == 'bwrap'`` the CLI runs inside bubblewrap
(see sandbox.py); it may additionally write its own state (~/.claude*).
"""

import shutil
import tempfile
from pathlib import Path
from typing import Any

from office_contracts import (
    AgentEventKind,
    AgentResult,
    Capability,
    CapabilityManifest,
    ConfigField,
)

from agent_adapters import sandbox
from agent_adapters.runner import AdapterRunError, RunContext, TaskBackedAdapter

DEFAULT_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep"]
# The CLI keeps sessions/auth/config here; it must be able to write them.
CLI_STATE = ["~/.claude", "~/.claude.json", "~/.claude.json.backup", "~/.cache/claude"]


def _real_cli(configured: str | None) -> str:
    if configured:
        return configured
    if found := shutil.which("claude"):
        return found
    import claude_agent_sdk

    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    if bundled.is_file():
        return str(bundled)
    raise AdapterRunError("cli_not_found", "Claude Code CLI not found", retryable=False)


class ClaudeCliAdapter(TaskBackedAdapter):
    adapter_type = "claude-cli"
    config_fields = [
        ConfigField(
            key="model",
            type="string",
            description="Model Claude; bỏ trống = mặc định của CLI.",
            example="claude-sonnet-5-5",
        ),
        ConfigField(
            key="effort",
            type="string",
            description="Mức suy nghĩ; cao hơn = kỹ hơn nhưng chậm và tốn hơn.",
            choices=["low", "medium", "high", "xhigh", "max"],
            example="high",
        ),
        ConfigField(
            key="system_prompt",
            type="string",
            description="Mô tả vai trò/bối cảnh, nối thêm vào prompt hệ thống.",
            example="Bạn là reviewer khó tính, ưu tiên test và bảo mật.",
        ),
        ConfigField(
            key="tools",
            type="string[]",
            description="Tool có sẵn của Claude Code khi làm task; [] = chỉ trả lời chữ.",
            default=DEFAULT_TOOLS,
            example=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
        ),
        ConfigField(
            key="max_turns",
            type="integer",
            description="Số lượt tối đa của agent trong một task.",
            default=30,
        ),
        ConfigField(
            key="permission_mode",
            type="string",
            description="Quyền sửa file; acceptEdits = tự sửa trong thư mục task.",
            choices=["default", "acceptEdits", "plan", "bypassPermissions"],
            default="acceptEdits",
        ),
        ConfigField(
            key="cli_path",
            type="string",
            description="Đường dẫn CLI `claude`; bỏ trống = tìm trong PATH.",
        ),
        ConfigField(
            key="capabilities",
            type="string[]",
            description="Việc worker nhận được; Điều phối giao task theo các nhãn này.",
            default=["chat", "code"],
            choices=[c.value for c in Capability],
        ),
    ]

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

    def build_options(self, ctx: RunContext, scratch: Path | None = None) -> Any:
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
        if inv.sandbox.mode != "none":
            options["cli_path"] = sandbox.write_wrapper_script(
                scratch or Path(tempfile.mkdtemp(prefix="claude-sbx-")),
                _real_cli(self.config.get("cli_path")),
                inv.sandbox,
                CLI_STATE,
            )
        elif self.config.get("cli_path"):
            options["cli_path"] = self.config["cli_path"]
        return ClaudeAgentOptions(**options)

    async def _execute(self, ctx: RunContext) -> AgentResult:
        from claude_agent_sdk import query

        query_fn = self._query_fn or query
        with tempfile.TemporaryDirectory(prefix="claude-sbx-") as scratch:
            result = await self._stream(ctx, query_fn, self.build_options(ctx, Path(scratch)))
        return self._result(ctx, result)

    async def _stream(self, ctx: RunContext, query_fn, options):
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

        result = None
        async for message in query_fn(prompt=ctx.invocation.instructions, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        await ctx.emit(AgentEventKind.MESSAGE, text=block.text)
                    elif isinstance(block, ToolUseBlock):
                        await ctx.emit(AgentEventKind.TOOL_CALL, name=block.name, input=block.input)
            elif isinstance(message, ResultMessage):
                result = message
                ctx.provider_ref = message.session_id
        return result

    def _result(self, ctx: RunContext, result) -> AgentResult:
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
