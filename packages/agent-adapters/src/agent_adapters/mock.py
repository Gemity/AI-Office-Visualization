"""Deterministic adapter for tests and demos (no provider calls).

Config:
  capabilities: list[str]       default ["chat", "code"]
  delay_s: float                simulated work time, default 0.05
  responses: list[{text, structured?, files?: {name: content}}]
                                consumed in order; the last one repeats.
                                Default: echo of the instructions.
  fail_first: int               fail the first N runs with a retryable error
  fail_code: str                error code for failures, default "mock_failure"
  fail_retryable: bool          default true
  permissions: list[str]        declared required_permissions (e.g. "deploy_production")
  cost_usd: float               cost reported for each successful run
"""

import asyncio
from pathlib import Path

from office_contracts import (
    AgentEventKind,
    AgentResult,
    Capability,
    CapabilityManifest,
    ConfigField,
)

from agent_adapters.runner import AdapterRunError, RunContext, TaskBackedAdapter


class MockAdapter(TaskBackedAdapter):
    """For tests and demos: answers from a script, never calls a provider."""

    adapter_type = "mock"
    config_fields = [
        ConfigField(
            key="responses",
            type="object[]",
            description="Kịch bản trả lời lần lượt (hết thì lặp cái cuối): text, files.",
            example=[{"text": "Đã xong", "files": {"notes.md": "# Ghi chú"}}],
        ),
        ConfigField(
            key="delay_s",
            type="number",
            description="Thời gian giả lập mỗi việc (giây).",
            default=0.05,
        ),
        ConfigField(
            key="permissions",
            type="string[]",
            description="Quyền khai báo (để thử approval rủi ro cao).",
            default=[],
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
        self._calls = 0

    def describe_capabilities(self) -> CapabilityManifest:
        return CapabilityManifest(
            adapter_type=self.adapter_type,
            provider="mock",
            capabilities=self.config.get("capabilities", ["chat", "code"]),
            invocation_modes=["async_job"],
            required_permissions=self.config.get("permissions", []),
            supported_artifact_types=["code", "document"],
            cost_model={"unit": "none"},
            observability_level="events",
            supports_cancel=True,
        )

    async def _execute(self, ctx: RunContext) -> AgentResult:
        self._calls += 1
        await asyncio.sleep(float(self.config.get("delay_s", 0.05)))
        if self._calls <= int(self.config.get("fail_first", 0)):
            raise AdapterRunError(
                self.config.get("fail_code", "mock_failure"),
                f"mock failure on call {self._calls}",
                retryable=self.config.get("fail_retryable", True),
            )
        responses = self.config.get("responses") or [
            {"text": f"done: {ctx.invocation.instructions[:200]}"}
        ]
        response = responses[min(self._calls - 1, len(responses) - 1)]
        for name, content in (response.get("files") or {}).items():
            if ctx.invocation.workdir:
                path = Path(ctx.invocation.workdir) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
        await ctx.emit(AgentEventKind.MESSAGE, text=response.get("text", ""))
        return AgentResult(
            text=response.get("text", ""),
            structured=response.get("structured"),
            cost_usd=self.config.get("cost_usd"),
        )
