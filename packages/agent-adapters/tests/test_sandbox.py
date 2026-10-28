import subprocess
from pathlib import Path

import pytest
from agent_adapters import sandbox
from agent_adapters.claude_sdk import ClaudeAgentSdkAdapter
from agent_adapters.codex_cli import CodexCliAdapter
from agent_adapters.runner import AdapterRunError, RunContext
from office_contracts import SandboxPolicy
from test_adapters import invocation


def _bwrap_works() -> bool:
    if sandbox.bwrap_path() is None:
        return False
    probe = subprocess.run(["bwrap", "--ro-bind", "/", "/", "true"], capture_output=True)
    return probe.returncode == 0


needs_bwrap = pytest.mark.skipif(not _bwrap_works(), reason="bubblewrap unavailable here")


def test_policy_none_leaves_command_untouched():
    assert sandbox.wrap(["codex", "exec"], SandboxPolicy()) == ["codex", "exec"]


def test_bwrap_args_mount_order(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "bwrap_path", lambda: "/usr/bin/bwrap")
    secret_dir, secret_file = tmp_path / "data", tmp_path / ".env"
    secret_dir.mkdir()
    secret_file.write_text("TOKEN=1")
    work = secret_dir / "ws" / "task"
    work.mkdir(parents=True)
    policy = SandboxPolicy(
        mode="bwrap",
        hidden=[str(secret_dir), str(secret_file), str(tmp_path / "missing")],
        readable=[str(secret_dir / "ws")],
        writable=[str(work)],
        network=False,
    )
    args = sandbox.bwrap_args(policy)
    assert args[:4] == ["/usr/bin/bwrap", "--ro-bind", "/", "/"] and args[-1] == "--"
    assert "--unshare-net" in args and "--die-with-parent" in args
    joined = " ".join(args)
    hide_dir = f"--tmpfs {secret_dir}"
    hide_file = f"--ro-bind /dev/null {secret_file}"
    reexpose = f"--ro-bind {secret_dir / 'ws'} {secret_dir / 'ws'}"
    write = f"--bind {work} {work}"
    assert str(tmp_path / "missing") not in joined
    # later mounts win: hide, then re-expose read-only, then writable
    assert joined.index(hide_dir) < joined.index(reexpose) < joined.index(write)
    assert hide_file in joined


def test_missing_bwrap_is_a_non_retryable_error(monkeypatch):
    monkeypatch.setattr(sandbox, "bwrap_path", lambda: None)
    with pytest.raises(AdapterRunError) as exc:
        sandbox.wrap(["x"], SandboxPolicy(mode="bwrap"))
    assert exc.value.error.code == "sandbox_unavailable" and not exc.value.error.retryable


@needs_bwrap
def test_bwrap_confines_writes_and_hides_secrets(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "key.txt").write_text("TOPSECRET")
    outside = Path(__file__).parent / "po-sandbox-escape.txt"  # host path, not under /tmp
    policy = SandboxPolicy(mode="bwrap", hidden=[str(secret)], writable=[str(work)])
    script = (
        f"echo ok > {work}/out.txt; "
        f"(echo x > {outside}) 2>/dev/null && echo WROTE || echo RO; "
        f"cat {secret}/key.txt 2>/dev/null || echo HIDDEN"
    )
    result = subprocess.run(
        sandbox.wrap(["sh", "-c", script], policy), capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["RO", "HIDDEN"]
    assert (work / "out.txt").read_text() == "ok\n"
    assert not outside.exists()


def test_codex_inside_bwrap_uses_outer_sandbox_only(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "bwrap_path", lambda: "/usr/bin/bwrap")
    adapter = CodexCliAdapter({})
    policy = SandboxPolicy(mode="bwrap", writable=[str(tmp_path)])
    ctx = RunContext("h", invocation(workdir=str(tmp_path), sandbox=policy))
    cmd = adapter.build_command(ctx, tmp_path)
    assert cmd[0] == "/usr/bin/bwrap" and "codex" in cmd
    assert cmd[cmd.index("-s") + 1] == "danger-full-access"
    plain = adapter.build_command(RunContext("h", invocation(workdir=str(tmp_path))), tmp_path)
    assert plain[0] == "codex" and plain[plain.index("-s") + 1] == "workspace-write"


def test_claude_uses_sandboxed_cli_wrapper(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "bwrap_path", lambda: "/usr/bin/bwrap")
    adapter = ClaudeAgentSdkAdapter({"cli_path": "/opt/claude"})
    policy = SandboxPolicy(mode="bwrap", writable=[str(tmp_path)])
    options = adapter.build_options(
        RunContext("h", invocation(workdir=str(tmp_path), sandbox=policy)), tmp_path
    )
    script = Path(options.cli_path).read_text()
    assert script.startswith("#!/bin/sh\nexec /usr/bin/bwrap ")
    assert "-- /opt/claude" in script and script.rstrip().endswith('"$@"')
