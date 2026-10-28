import asyncio
import stat
import sys
from pathlib import Path

import pytest
from agent_adapters import (
    AgentAdapter,
    SupportsArtifacts,
    SupportsCancel,
    SupportsEvents,
    create_adapter,
)
from agent_adapters.claude_sdk import ClaudeAgentSdkAdapter
from office_contracts import AgentInvocation, RunState

TERMINAL = {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}


def invocation(**overrides) -> AgentInvocation:
    base = dict(
        invocation_id="inv_1",
        idempotency_key="task_1:1",
        project_id="prj_1",
        agent_id="agt_1",
        trace_id="trace_1",
        mode="async_job",
        instructions="write hello",
    )
    return AgentInvocation(**{**base, **overrides})


async def wait_terminal(adapter, handle_id, timeout=5.0):
    async def _poll():
        while (status := await adapter.get_status(handle_id)).state not in TERMINAL:
            await asyncio.sleep(0.01)
        return status

    return await asyncio.wait_for(_poll(), timeout)


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("adapter_type", ["mock", "claude-agent-sdk", "codex-cli"])
def test_all_adapters_implement_full_contract(adapter_type):
    adapter = create_adapter(adapter_type)
    for proto in (AgentAdapter, SupportsCancel, SupportsEvents, SupportsArtifacts):
        assert isinstance(adapter, proto)
    assert adapter.describe_capabilities().adapter_type == adapter_type


def test_unknown_adapter_and_unsafe_codex_sandbox_rejected():
    with pytest.raises(ValueError):
        create_adapter("nope")
    with pytest.raises(ValueError):
        create_adapter("codex-cli", {"sandbox": "danger-full-access"})


def test_mock_run_events_artifacts_and_idempotency(tmp_path):
    async def main():
        adapter = create_adapter(
            "mock", {"responses": [{"text": "ok", "files": {"out/hello.py": "print(1)"}}]}
        )
        inv = invocation(workdir=str(tmp_path))
        h1 = await adapter.invoke(inv)
        h2 = await adapter.invoke(inv)
        assert h1.handle_id == h2.handle_id
        status = await wait_terminal(adapter, h1.handle_id)
        assert status.state == RunState.SUCCEEDED
        assert status.result.text == "ok"
        kinds = [e.kind async for e in adapter.subscribe_events(h1.handle_id)]
        assert kinds == ["status", "message"]
        [artifact] = await adapter.collect_artifacts(h1.handle_id)
        assert artifact.type == "code"
        assert artifact.uri.endswith("out/hello.py")
        assert artifact.checksum.startswith("sha256:")

    run(main())


def test_cancel_is_requested_then_confirmed():
    async def main():
        adapter = create_adapter("mock", {"delay_s": 5})
        handle = await adapter.invoke(invocation())
        await asyncio.sleep(0.01)
        result = await adapter.cancel(handle.handle_id)
        assert result.accepted and result.state == RunState.CANCEL_REQUESTED
        assert (await wait_terminal(adapter, handle.handle_id)).state == RunState.CANCELLED
        again = await adapter.cancel(handle.handle_id)
        assert not again.accepted and again.state == RunState.CANCELLED

    run(main())


def test_mock_failure_is_coded_and_retryable():
    async def main():
        adapter = create_adapter("mock", {"fail_first": 1})
        status = await wait_terminal(adapter, (await adapter.invoke(invocation())).handle_id)
        assert status.state == RunState.FAILED
        assert status.error.code == "mock_failure" and status.error.retryable

    run(main())


# --- Claude Agent SDK (fake query function) ----------------------------------


def test_claude_adapter_maps_stream_and_structured_output(tmp_path):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

    seen = {}

    async def fake_query(*, prompt, options):
        seen["prompt"], seen["options"] = prompt, options
        yield AssistantMessage(
            content=[TextBlock("planning"), ToolUseBlock("t1", "Write", {"file_path": "a.py"})],
            model="claude",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="sess_1",
            result="done",
            structured_output={"reply": "hi", "tasks": []},
            total_cost_usd=0.01,
        )

    async def main():
        adapter = ClaudeAgentSdkAdapter({"model": "m", "tools": []}, query_fn=fake_query)
        schema = {"type": "object"}
        inv = invocation(workdir=str(tmp_path), output_schema=schema)
        handle = await adapter.invoke(inv)
        status = await wait_terminal(adapter, handle.handle_id)
        assert status.state == RunState.SUCCEEDED, status.error
        assert status.result.structured == {"reply": "hi", "tasks": []}
        assert status.result.cost_usd == 0.01
        events = [e async for e in adapter.subscribe_events(handle.handle_id)]
        assert [e.kind for e in events] == ["status", "message", "tool_call"]
        opts = seen["options"]
        assert opts.cwd == str(tmp_path) and opts.model == "m" and opts.tools == []
        assert opts.output_format == {"type": "json_schema", "schema": schema}
        assert opts.setting_sources == []

    run(main())


def test_claude_adapter_error_result_fails_run():
    from claude_agent_sdk import ResultMessage

    async def fake_query(*, prompt, options):
        yield ResultMessage(
            subtype="error_max_turns",
            duration_ms=1,
            duration_api_ms=1,
            is_error=True,
            num_turns=30,
            session_id="s",
        )

    async def main():
        adapter = ClaudeAgentSdkAdapter({}, query_fn=fake_query)
        status = await wait_terminal(adapter, (await adapter.invoke(invocation())).handle_id)
        assert status.state == RunState.FAILED
        assert status.error.code == "error_max_turns"

    run(main())


# --- Codex CLI (fake executable) ---------------------------------------------

# ruff: noqa: E501 (fake CLI script below)
FAKE_CODEX = """#!{python}
import json, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
out = args[args.index("-o") + 1]
mode = {mode!r}
print(json.dumps({{"type": "thread.started", "thread_id": "th_1"}}), flush=True)
print(json.dumps({{"type": "item.started", "item": {{"type": "command_execution", "command": "ls"}}}}))
if mode == "fail":
    print(json.dumps({{"type": "turn.failed", "error": {{"message": "quota exceeded"}}}}))
    sys.exit(1)
text = json.dumps({{"echo": prompt, "args": args}}) if "--output-schema" in args else "done: " + prompt
print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": text}}}}))
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 5}}}}))
open(out, "w").write(text)
"""


def fake_codex(tmp_path: Path, mode: str = "ok") -> str:
    path = tmp_path / f"codex-{mode}"
    path.write_text(FAKE_CODEX.format(python=sys.executable, mode=mode))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_codex_adapter_runs_cli_and_parses_events(tmp_path):
    async def main():
        adapter = create_adapter("codex-cli", {"cli_path": fake_codex(tmp_path), "model": "gpt"})
        handle = await adapter.invoke(invocation(workdir=str(tmp_path)))
        status = await wait_terminal(adapter, handle.handle_id)
        assert status.state == RunState.SUCCEEDED, status.error
        assert status.result.text == "done: write hello"
        kinds = [e.kind async for e in adapter.subscribe_events(handle.handle_id)]
        assert kinds == ["status", "tool_call", "message", "progress"]

        handle = await adapter.invoke(
            invocation(
                idempotency_key="k2", workdir=str(tmp_path), output_schema={"type": "object"}
            )
        )
        status = await wait_terminal(adapter, handle.handle_id)
        args = status.result.structured["args"]
        assert args[args.index("-C") + 1] == str(tmp_path)
        assert args[args.index("-s") + 1] == "workspace-write"
        assert args[args.index("-m") + 1] == "gpt"
        assert "--output-schema" in args and "--json" in args

    run(main())


def test_codex_adapter_reports_turn_failure(tmp_path):
    async def main():
        adapter = create_adapter("codex-cli", {"cli_path": fake_codex(tmp_path, "fail")})
        status = await wait_terminal(adapter, (await adapter.invoke(invocation())).handle_id)
        assert status.state == RunState.FAILED
        assert "quota exceeded" in status.error.message

    run(main())


def test_codex_missing_cli_is_not_retryable():
    async def main():
        adapter = create_adapter("codex-cli", {"cli_path": "/nonexistent/codex"})
        status = await wait_terminal(adapter, (await adapter.invoke(invocation())).handle_id)
        assert status.error.code == "cli_not_found" and not status.error.retryable

    run(main())
