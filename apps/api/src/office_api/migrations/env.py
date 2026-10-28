from alembic import context

from office_api import models  # noqa: F401  (registers tables on Base.metadata)
from office_api.db import Base

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("run migrations through office_api.migrations (needs a connection)")

context.configure(
    connection=connection,
    target_metadata=Base.metadata,
    render_as_batch=True,  # SQLite cannot ALTER most things in place
    compare_type=True,
)
with context.begin_transaction():
    context.run_migrations()
