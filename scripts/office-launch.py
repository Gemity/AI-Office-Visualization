#!/usr/bin/env python3
"""Bấm là chạy: mở AI Project Office cho repo đang chứa file này.

Chỉ cần mang đúng file này: đặt vào `<repo>/.claude/` hoặc `<repo>/.agents/` (hoặc gốc
repo), `chmod +x`, rồi double-click hoặc chạy `./office-launch.py`. Mỗi lần chạy:
1. khởi động server nếu chưa chạy (systemd `--user` nếu đã cài, không thì chạy nền);
2. lần đầu: tạo Owner, lưu token vào ~/.config/ai-project-office/token (chmod 600);
3. tìm hoặc tạo project gắn với repo này;
4. đảm bảo project có Điều phối đúng như CẤU HÌNH bên dưới (khác thì thay bằng
   phiên bản mới, có ghi lịch sử), rồi mở trình duyệt — chat được ngay.

Tùy chọn: `--stop` dừng server, `--no-browser` chỉ in URL. Python ≥ 3.10, chỉ thư viện chuẩn.
"""

# ================================ CẤU HÌNH ================================
# Sửa trực tiếp ở đây. Lần chạy sau sẽ áp dụng.

# Nơi cài AI Project Office (repo chứa pyproject.toml) và cổng server.
OFFICE_HOME = "~/UnityProject/AI-Office-Visualization"
PORT = 8000

# Điều phối viên của project này. Owner chỉ chat với agent này.
ORCHESTRATOR = {
    "name": "Điều phối",
    # Adapter: "claude-cli" (Claude Code CLI `claude`) | "codex-cli" (Codex CLI `codex`).
    "adapter": "claude-cli",
    # Config của adapter (không để secret ở đây). Bỏ dấu # để dùng.
    # claude-cli: model, effort (low|medium|high|xhigh|max), system_prompt, max_turns, cli_path
    # codex-cli:  model, extra_args, cli_path
    # Mô tả đầy đủ từng khóa: tab Agent trong UI, hoặc GET /adapters (config_fields).
    "config": {
        # "model": "claude-opus-5-5",
        # "effort": "high",
        # "system_prompt": "Bối cảnh riêng của repo này: ...",
    },
}
# ==========================================================================

import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402
import uuid  # noqa: E402
import webbrowser  # noqa: E402
from pathlib import Path  # noqa: E402

HOME_DIR = Path(OFFICE_HOME).expanduser()
BASE = f"http://127.0.0.1:{PORT}"
# Same locations as infra/systemd/ai-project-office.service.
SHARE = Path.home() / ".local/share/ai-project-office"
DB = SHARE / "office.db"
CONFIG = Path.home() / ".config/ai-project-office"
TOKEN_FILE = CONFIG / "token"
PID_FILE = SHARE / "launcher.pid"
LOG_FILE = SHARE / "server.log"
UNIT = Path.home() / ".config/systemd/user/ai-project-office.service"


class LaunchError(Exception):
    pass


# ---------------------------------------------------------------- messages


def notify(text: str) -> None:
    print(text)
    if shutil.which("notify-send") and not sys.stdout.isatty():
        subprocess.run(["notify-send", "AI Project Office", text], check=False)


def fail(text: str) -> None:
    print(f"LỖI: {text}", file=sys.stderr)
    if shutil.which("zenity") and not sys.stdout.isatty():
        subprocess.run(["zenity", "--error", "--title=AI Project Office", f"--text={text}"])
    sys.exit(1)


def ask_token() -> str:
    prompt = "DB đã có Owner. Dán token Owner (po_…) đã tạo trước đây:"
    if shutil.which("zenity") and not sys.stdin.isatty():
        r = subprocess.run(
            ["zenity", "--entry", "--hide-text", "--title=AI Project Office", f"--text={prompt}"],
            capture_output=True,
            text=True,
        )
        return r.stdout.strip()
    import getpass

    return getpass.getpass(prompt + " ").strip()


# ---------------------------------------------------------------- server


def server_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("OFFICE_DATABASE_URL", f"sqlite+aiosqlite:///{DB}")
    env.setdefault("OFFICE_DATA_DIR", str(SHARE / "data"))
    env["PATH"] = f"{Path.home() / '.local/bin'}:{env.get('PATH', '/usr/bin:/bin')}"
    return env


def uv() -> str:
    path = shutil.which("uv", path=server_env()["PATH"])
    if not path:
        raise LaunchError("Không tìm thấy `uv` (cài: https://docs.astral.sh/uv/).")
    return path


def ready() -> bool:
    """True if our server answers; LaunchError if something else holds the port."""
    try:
        with urllib.request.urlopen(f"{BASE}/readyz", timeout=2) as r:
            body = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 503:
            return False  # ours, still starting
        raise LaunchError(f"Cổng {PORT} đang bị chương trình khác dùng (HTTP {e.code}).") from None
    except (urllib.error.URLError, OSError):
        return False
    except ValueError:
        raise LaunchError(f"Cổng {PORT} đang bị chương trình khác dùng.") from None
    if body.get("status") != "ready":
        raise LaunchError(f"Cổng {PORT} đang bị chương trình khác dùng.")
    return True


def start_server() -> None:
    if ready():
        return
    if not (HOME_DIR / "pyproject.toml").is_file():
        raise LaunchError(f"Không thấy AI Project Office ở {HOME_DIR}. Sửa OFFICE_HOME ở đầu file.")
    SHARE.mkdir(parents=True, exist_ok=True)
    if UNIT.is_file():
        notify("Đang khởi động service ai-project-office…")
        subprocess.run(["systemctl", "--user", "start", "ai-project-office"], check=True)
    else:
        notify("Đang khởi động server…")
        log = open(LOG_FILE, "ab")
        proc = subprocess.Popen(
            [uv(), "run", "office-api", "serve", "--host", "127.0.0.1", "--port", str(PORT)],
            cwd=HOME_DIR,  # office-policy.toml is read from here
            env=server_env(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,  # keeps running after the launcher exits
        )
        PID_FILE.write_text(str(proc.pid))
    deadline = time.monotonic() + 120  # first run also installs dependencies
    while time.monotonic() < deadline:
        if ready():
            return
        time.sleep(0.5)
    where = "journalctl --user -u ai-project-office" if UNIT.is_file() else str(LOG_FILE)
    raise LaunchError(f"Server không lên sau 120 giây. Xem log: {where}")


def stop_server() -> None:
    if UNIT.is_file():
        subprocess.run(["systemctl", "--user", "stop", "ai-project-office"], check=False)
    elif PID_FILE.is_file():
        try:
            os.killpg(int(PID_FILE.read_text()), signal.SIGTERM)  # graceful: jobs are detached
        except (ProcessLookupError, ValueError):
            pass
        PID_FILE.unlink(missing_ok=True)
    notify("Đã dừng AI Project Office.")


# ---------------------------------------------------------------- API


def api(method: str, path: str, token: str, body: dict | None = None):
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        headers |= {"Content-Type": "application/json", "Idempotency-Key": str(uuid.uuid4())}
        data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def token_valid(token: str) -> bool:
    try:
        api("GET", "/projects", token)
        return True
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False
        raise


def has_principals() -> bool:
    if not DB.is_file():
        return False
    with sqlite3.connect(DB) as db:
        return db.execute("SELECT count(*) FROM principals").fetchone()[0] > 0


def save_token(token: str) -> None:
    CONFIG.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.touch(mode=0o600)
    TOKEN_FILE.chmod(0o600)
    TOKEN_FILE.write_text(token)


def get_token() -> str:
    if TOKEN_FILE.is_file():
        token = TOKEN_FILE.read_text().strip()
        if token_valid(token):
            return token
    if not has_principals():
        out = subprocess.run(
            [uv(), "run", "office-api", "create-owner", "--name", os.environ.get("USER", "owner")],
            cwd=HOME_DIR,
            env=server_env(),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        token = next(line.split()[-1] for line in out.splitlines() if line.startswith("token:"))
        notify(f"Đã tạo Owner; token lưu ở {TOKEN_FILE}")
    else:
        token = ask_token()
        if not token or not token_valid(token):
            raise LaunchError("Token không hợp lệ.")
    save_token(token)
    return token


def find_repo() -> Path:
    here = Path(__file__).resolve().parent
    for p in [here, *here.parents]:
        if (p / ".git").exists():
            return p
    return here.parent if here.name in {".claude", ".agents"} else here


def project_for(repo: Path, token: str) -> str:
    for p in api("GET", "/projects", token):
        if p["config"].get("repo_path") == str(repo):
            return p["id"]
    body = {"name": repo.name, "config": {"repo_path": str(repo)}}
    created = api("POST", "/projects", token, body)
    notify(f"Đã tạo project “{repo.name}”.")
    return created["data"]["id"]


def ensure_orchestrator(pid: str, token: str) -> None:
    """The project's active Orchestrator must match ORCHESTRATOR; otherwise a new
    version replaces it (BR-01: versioned, audited by the server)."""
    want = {
        "name": ORCHESTRATOR["name"],
        "adapter_type": ORCHESTRATOR["adapter"],
        "config": ORCHESTRATOR.get("config") or {},
    }
    agents = api("GET", f"/projects/{pid}/agents", token)
    active = next((a for a in agents if a["is_active_orchestrator"]), None)
    if active and all(active[k] == v for k, v in want.items()):
        return
    try:
        api("POST", f"/projects/{pid}/agents", token, {**want, "role": "orchestrator"})
    except urllib.error.HTTPError as e:
        detail = json.loads(e.read() or b"{}").get("error", {}).get("message", e.reason)
        raise LaunchError(f"Cấu hình ORCHESTRATOR không hợp lệ: {detail}") from None
    notify("Đã cập nhật Điều phối theo cấu hình." if active else "Đã tạo Điều phối.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Mở AI Project Office cho repo này.")
    parser.add_argument("--stop", action="store_true", help="dừng server")
    parser.add_argument("--no-browser", action="store_true", help="chỉ in URL, không mở browser")
    args = parser.parse_args()
    try:
        if args.stop:
            stop_server()
            return
        start_server()
        token = get_token()
        repo = find_repo()
        pid = project_for(repo, token)
        ensure_orchestrator(pid, token)
    except LaunchError as e:
        fail(str(e))
    except (subprocess.CalledProcessError, urllib.error.URLError, OSError) as e:
        fail(f"{type(e).__name__}: {e}")
    if args.no_browser:
        print(f"{BASE}/  (project {pid}, repo {repo})")
    else:
        webbrowser.open(f"{BASE}/#token={token}&project={pid}")
        notify(f"Đã mở project “{repo.name}”.")


if __name__ == "__main__":
    main()
