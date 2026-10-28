"""bubblewrap (bwrap) wrapper for adapters that run local CLI processes (spec §13).

Layout inside the sandbox, in mount order (later mounts win):
  /            read-only bind of the host root
  /dev /proc   fresh devtmpfs / procfs;  /tmp  empty tmpfs
  hidden       directories → empty tmpfs, files → /dev/null
  readable     read-only binds re-exposed inside hidden paths
  writable     read-write binds (task workdir, the CLI's own state)
The process gets its own PID namespace and session and dies with its parent.
Network is shared unless the policy disables it (provider CLIs call their API).
"""

import shlex
import shutil
import stat
from pathlib import Path

from office_contracts import SandboxPolicy

from agent_adapters.runner import AdapterRunError


def bwrap_path() -> str | None:
    return shutil.which("bwrap")


def bwrap_args(policy: SandboxPolicy, extra_writable: list[str] = ()) -> list[str]:
    """Returns the bwrap argv prefix (ending with ``--``) for ``policy``."""
    exe = bwrap_path()
    if exe is None:
        raise AdapterRunError(
            "sandbox_unavailable",
            "bubblewrap (bwrap) is not installed; install it or set OFFICE_SANDBOX=none",
            retryable=False,
        )
    args = [
        exe,
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--unshare-pid",
        "--unshare-ipc",
        "--new-session",
        "--die-with-parent",
    ]  # fmt: skip
    if not policy.network:
        args.append("--unshare-net")
    for raw in policy.hidden:
        path = Path(raw).expanduser()
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_dir():
            args += ["--tmpfs", str(path)]
        elif stat.S_ISREG(path.lstat().st_mode):
            args += ["--ro-bind", "/dev/null", str(path)]
    for raw in policy.readable:
        path = Path(raw).expanduser()
        if path.exists():
            args += ["--ro-bind", str(path), str(path)]
    for raw in [*policy.writable, *extra_writable]:
        path = Path(raw).expanduser()
        if path.exists():
            args += ["--bind", str(path), str(path)]
    return [*args, "--"]


def wrap(argv: list[str], policy: SandboxPolicy, extra_writable: list[str] = ()) -> list[str]:
    if policy.mode == "none":
        return argv
    return [*bwrap_args(policy, extra_writable), *argv]


def write_wrapper_script(
    directory: Path, cli: str, policy: SandboxPolicy, extra_writable: list[str] = ()
) -> str:
    """For SDKs that spawn a CLI themselves (``cli_path``): a script that execs
    the real CLI inside the sandbox with the SDK's arguments."""
    script = directory / "sandboxed-cli"
    command = shlex.join(wrap([cli], policy, extra_writable))
    script.write_text(f'#!/bin/sh\nexec {command} "$@"\n')
    script.chmod(0o700)
    return str(script)
