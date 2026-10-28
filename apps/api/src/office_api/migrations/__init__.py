"""Alembic migrations (ADR-0006). The app upgrades to head on startup; run
``office-api migrate`` to do it explicitly, ``office-api makemigration -m ...``
to generate a revision after changing models.py."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Connection

HERE = Path(__file__).parent


def config(connection: Connection | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(HERE))
    cfg.attributes["connection"] = connection
    return cfg


def upgrade(connection: Connection) -> None:
    tables = set(inspect(connection).get_table_names())
    if tables and "alembic_version" not in tables:
        raise RuntimeError(
            "Database was created before migrations existed (create_all). "
            "Delete the old dev database or stamp it after checking its schema."
        )
    command.upgrade(config(connection), "head")
