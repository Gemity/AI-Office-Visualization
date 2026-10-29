"""office-api CLI.

office-api create-owner --name "Quy"   # prints a bearer token once
office-api serve [--host 127.0.0.1] [--port 8000]
office-api migrate                      # upgrade the database to the latest schema
office-api makemigration -m "message"  # new Alembic revision from models.py changes
office-api backup [--keep N]            # snapshot DB + data dir into <data>/backups
office-api prune [--dry-run]            # apply the [retention] policy now
"""

import argparse
import asyncio
import sqlite3
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from office_api import migrations
from office_api.auth import create_principal_with_token
from office_api.config import Settings
from office_api.db import create_engine, create_sessionmaker
from office_api.logs import log_config
from office_api.storage import ContentStore


async def migrate(url: str) -> None:
    engine = create_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(migrations.upgrade)
    await engine.dispose()


async def _create_owner(name: str) -> None:
    settings = Settings()
    await migrate(settings.database_url)
    engine = create_engine(settings.database_url)
    async with create_sessionmaker(engine)() as session:
        principal, token = await create_principal_with_token(session, name)
    await engine.dispose()
    print(f"principal_id: {principal.id}")
    print(f"token:        {token}")
    print("Store the token now; only its hash is kept.")


async def _make_migration(message: str) -> None:
    """Diffs models.py against a scratch database at the current head."""
    from alembic import command

    with tempfile.TemporaryDirectory() as tmp:
        engine = create_engine(f"sqlite+aiosqlite:///{tmp}/head.db")
        async with engine.begin() as conn:
            await conn.run_sync(migrations.upgrade)
            await conn.run_sync(
                lambda c: command.revision(migrations.config(c), message=message, autogenerate=True)
            )
        await engine.dispose()


async def _prune(dry_run: bool) -> None:
    from office_api import retention

    settings = Settings()
    if not settings.policy.retention.enabled:
        print("retention is off (content_days = workspace_days = 0 in the policy file)")
        return
    await migrate(settings.database_url)
    engine = create_engine(settings.database_url)
    async with create_sessionmaker(engine)() as session:
        pruned = await retention.sweep(
            session, settings, ContentStore(settings.data_dir), dry_run=dry_run
        )
    await engine.dispose()
    verb = "would delete" if dry_run else "deleted"
    for p in pruned:
        print(f"{p.project_id}: {verb} {len(p.objects)} objects, {len(p.workspaces)} workspaces")
    if not pruned:
        print("nothing to prune")


def backup(settings: Settings, keep: int) -> Path:
    """Consistent SQLite snapshot (online backup API) plus the data directory
    (content store, workspaces) in one tar.gz under <data>/backups."""
    url = make_url(settings.database_url)
    if url.get_backend_name() != "sqlite":
        raise SystemExit("backup supports SQLite only; use pg_dump for PostgreSQL")
    data = Path(settings.data_dir).resolve()
    dest = data / "backups"
    dest.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    archive = dest / f"office-{stamp}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "office.db"
        src = sqlite3.connect(url.database)
        dst = sqlite3.connect(snapshot)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(snapshot, arcname="office.db")
            for child in data.iterdir():
                if child.name != "backups":
                    tar.add(child, arcname=f"data/{child.name}")
    archive.chmod(0o600)
    for old in sorted(dest.glob("office-*.tar.gz"))[:-keep]:
        old.unlink()
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(prog="office-api")
    sub = parser.add_subparsers(dest="cmd", required=True)
    owner = sub.add_parser("create-owner", help="create a principal and print its API token")
    owner.add_argument("--name", required=True)
    serve = sub.add_parser("serve", help="run the API server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("migrate", help="upgrade the database schema to head")
    mk = sub.add_parser("makemigration", help="generate a migration from model changes")
    mk.add_argument("-m", "--message", required=True)
    bk = sub.add_parser("backup", help="snapshot the database and data directory")
    bk.add_argument(
        "--keep", type=int, help="backups to keep (default: retention.backups_keep in the policy)"
    )
    pr = sub.add_parser("prune", help="apply the retention policy now")
    pr.add_argument("--dry-run", action="store_true", help="only list what would be deleted")
    args = parser.parse_args()

    if args.cmd == "create-owner":
        asyncio.run(_create_owner(args.name))
    elif args.cmd == "migrate":
        asyncio.run(migrate(Settings().database_url))
        print("database is at the latest schema")
    elif args.cmd == "makemigration":
        asyncio.run(_make_migration(args.message))
    elif args.cmd == "backup":
        settings = Settings()
        print(backup(settings, args.keep or settings.policy.retention.backups_keep))
    elif args.cmd == "prune":
        asyncio.run(_prune(args.dry_run))
    elif args.cmd == "serve":
        import uvicorn

        from office_api.main import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port, log_config=log_config())


if __name__ == "__main__":
    main()
