"""Local content store for ``content_ref`` values (``obj://<id>``).

Message bodies and run outputs live here instead of in DB rows/events
(spec §9). Swap for S3-compatible object storage later; refs stay opaque.
"""

from pathlib import Path

from office_contracts import SandboxPolicy
from sqlalchemy.engine import make_url

from office_api.db import new_id

PREFIX = "obj://"


class ContentStore:
    def __init__(self, data_dir: str):
        self.root = Path(data_dir) / "objects"
        self.root.mkdir(parents=True, exist_ok=True)

    def put_text(self, text: str) -> str:
        object_id = new_id("obj")
        (self.root / f"{object_id}.txt").write_text(text, encoding="utf-8")
        return PREFIX + object_id

    def get_text(self, ref: str | None) -> str | None:
        if not ref:
            return None
        if not ref.startswith(PREFIX):
            raise ValueError(f"unsupported content_ref {ref!r}")
        object_id = ref.removeprefix(PREFIX)
        if not object_id.replace("_", "").isalnum():
            raise ValueError(f"invalid content_ref {ref!r}")
        path = self.root / f"{object_id}.txt"
        return path.read_text(encoding="utf-8") if path.exists() else None


def workspace_root(data_dir: str, project_id: str) -> Path:
    return (Path(data_dir) / "workspaces" / project_id).resolve()


def preview(text: str | None, limit: int = 200) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# Never visible to sandboxed agents (besides the office's own data, see sandbox_policy).
SENSITIVE_HOME_PATHS = [
    "~/.ssh", "~/.gnupg", "~/.aws", "~/.azure", "~/.kube", "~/.docker", "~/.config/gh",
    "~/.config/gcloud", "~/.netrc", "~/.git-credentials", "~/.pgpass", "~/.password-store",
]  # fmt: skip


def sandbox_policy(settings, project_id: str, workdir: Path | None) -> SandboxPolicy:
    """Agents may read the host read-only, minus secrets and the office's own data;
    of the office data they see only their project's workspace (BR-04)."""
    if settings.sandbox == "none":
        return SandboxPolicy(mode="none")
    hidden = [str(Path(settings.data_dir).resolve()), *SENSITIVE_HOME_PATHS]
    url = make_url(settings.database_url)
    if url.get_backend_name() == "sqlite" and url.database not in (None, "", ":memory:"):
        db = Path(url.database).resolve()
        hidden += [str(db), f"{db}-wal", f"{db}-shm", f"{db}-journal"]
    cwd = Path.cwd()
    hidden += [str(p) for p in cwd.glob(".env*")] + [str(cwd / "secrets")]
    return SandboxPolicy(
        mode="bwrap",
        hidden=hidden,
        readable=[str(workspace_root(settings.data_dir, project_id))],
        writable=[str(workdir)] if workdir else [],
    )
