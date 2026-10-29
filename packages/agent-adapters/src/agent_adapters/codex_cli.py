"""OpenAI Codex via ``codex exec`` (non-interactive CLI, logged-in user).

Each run is one ``codex exec --json`` subprocess in the task's workdir with the
``workspace-write`` sandbox. JSONL events on stdout become AgentEvents; the
final message comes from ``--output-last-message``.

Config:
  model: str                  ``-m``; default: Codex default
  sandbox: str                read-only | workspace-write (default)
  capabilities: list[str]     default ["chat", "code"]
  cli_path: str               default "codex"
  extra_args: list[str]       appended before the prompt

With ``invocation.sandbox.mode == 'bwrap'`` the CLI runs inside bubblewrap
(see sandbox.py) and bubblewrap is the only isolation layer: Codex's own Linux
sandbox needs a nested user namespace, which hosts that restrict unprivileged
user namespaces (Ubuntu AppArmor) refuse inside another sandbox. Codex is then
started with ``danger-full-access`` *inside* bwrap: it can write only the
workdir, its scratch dir and ~/.codex, and cannot see hidden paths; its shell
commands do have network access (ADR-0006).
"""

import asyncio
import json
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

CLI_STATE = ["~/.codex"]
ALLOWED_SANDBOXES = {"read-only", "workspace-write"}  # never danger-full-access


class CodexCliAdapter(TaskBackedAdapter):
    adapter_type = "codex-cli"
    config_fields = [
        ConfigField(
            key="model",
            type="string",
            description="Model Codex (`-m`); bỏ trống = mặc định của Codex.",
            example="gpt-5-codex",
        ),
        ConfigField(
            key="sandbox",
            type="string",
            description="Quyền ghi file của Codex trong thư mục task.",
            choices=sorted(ALLOWED_SANDBOXES),
            default="workspace-write",
        ),
        ConfigField(
            key="extra_args",
            type="string[]",
            description="Tham số thêm cho `codex exec`, đặt trước prompt.",
            example=["-c", "model_reasoning_effort=high"],
        ),
        ConfigField(
            key="cli_path",
            type="string",
            description="Đường dẫn CLI `codex`.",
            default="codex",
        ),
        ConfigField(
            key="capabilities",
            type="string[]",
            description="Việc worker nhận được; Điều phối giao task theo các nhãn này.",
            default=["chat", "code"],
            choices=[c.value for c in Capability],
        ),
    ]

    def __init__(self, config=None):
        super().__init__(config)
        if self.config.get("sandbox", "workspace-write") not in ALLOWED_SANDBOXES:
            raise ValueError(f"sandbox must be one of {sorted(ALLOWED_SANDBOXES)}")

    def describe_capabilities(self) -> CapabilityManifest:
        return CapabilityManifest(
            adapter_type=self.adapter_type,
            provider="openai",
            capabilities=self.config.get("capabilities", ["chat", "code"]),
            invocation_modes=["stream"],
            required_permissions=["fs.workdir.write", "shell.sandboxed"],
            supported_artifact_types=["code", "document"],
            cost_model={"unit": "token", "notes": "usage reported per turn; price not exposed"},
            observability_level="events",
            supports_cancel=True,
        )

    def build_command(self, ctx: RunContext, tmp: Path) -> list[str]:
        inv = ctx.invocation
        cmd = [
            self.config.get("cli_path", "codex"),
            "exec",
            "--json",
            "--color",
            "never",
            "--skip-git-repo-check",
            "--ephemeral",
            "-s",
            self._codex_sandbox(inv),
            "-o",
            str(tmp / "last_message.txt"),
            "-C",
            inv.workdir or str(tmp),
        ]
        if self.config.get("model"):
            cmd += ["-m", self.config["model"]]
        if inv.output_schema is not None:
            schema_path = tmp / "schema.json"
            schema_path.write_text(json.dumps(inv.output_schema))
            cmd += ["--output-schema", str(schema_path)]
        cmd += list(self.config.get("extra_args", []))
        cmd.append("-")  # prompt from stdin: no argv length limits, not visible in ps
        return sandbox.wrap(cmd, inv.sandbox, [str(tmp), *CLI_STATE])

    def _codex_sandbox(self, inv) -> str:
        if inv.sandbox.mode == "bwrap":
            return "danger-full-access"  # confined by the outer bwrap sandbox instead
        # No workdir (e.g. an Orchestrator turn): read-only in a scratch dir.
        return self.config.get("sandbox", "workspace-write") if inv.workdir else "read-only"

    async def _execute(self, ctx: RunContext) -> AgentResult:
        with tempfile.TemporaryDirectory(prefix="codex-run-") as tmpdir:
            tmp = Path(tmpdir)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self.build_command(ctx, tmp),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                raise AdapterRunError("cli_not_found", "codex CLI not found", False) from None
            try:
                proc.stdin.write(ctx.invocation.instructions.encode())
                await proc.stdin.drain()
                proc.stdin.close()
                failure = await self._read_events(ctx, proc.stdout)
                stderr = (await proc.stderr.read()).decode(errors="replace")
                code = await proc.wait()
            except asyncio.CancelledError:
                if proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), 10)
                    except TimeoutError:
                        proc.kill()
                raise

            if failure or code != 0:
                raise AdapterRunError(
                    "codex_failed", failure or stderr[-2000:] or f"exit {code}", retryable=True
                )
            last = tmp / "last_message.txt"
            text = last.read_text().strip() if last.exists() else ""

        structured = None
        if ctx.invocation.output_schema is not None:
            try:
                structured = json.loads(text)
            except json.JSONDecodeError:
                raise AdapterRunError(
                    "bad_structured_output", "final message is not JSON", True
                ) from None
            if not isinstance(structured, dict):
                raise AdapterRunError(
                    "bad_structured_output", "final message is not an object", True
                )
        return AgentResult(text=text, structured=structured)

    async def _read_events(self, ctx: RunContext, stdout: asyncio.StreamReader) -> str | None:
        """Maps JSONL events; returns an error message if the turn failed."""
        failure = None
        async for raw in stdout:
            try:
                event: dict[str, Any] = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = event.get("type", "")
            item = event.get("item") or {}
            if kind == "thread.started":
                ctx.provider_ref = event.get("thread_id")
            elif kind == "item.completed" and item.get("type") == "agent_message":
                await ctx.emit(AgentEventKind.MESSAGE, text=item.get("text", ""))
            elif kind == "item.started" and item.get("type") == "command_execution":
                await ctx.emit(
                    AgentEventKind.TOOL_CALL, name="shell", input={"command": item.get("command")}
                )
            elif kind == "item.completed" and item.get("type") == "file_change":
                await ctx.emit(
                    AgentEventKind.TOOL_CALL,
                    name="file_change",
                    input={"changes": item.get("changes")},
                )
            elif kind == "turn.completed":
                await ctx.emit(AgentEventKind.PROGRESS, usage=event.get("usage"))
            elif kind in {"turn.failed", "error"}:
                error = event.get("error") or {}
                failure = error.get("message") or event.get("message") or kind
        return failure
