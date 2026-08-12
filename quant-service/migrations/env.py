from __future__ import annotations

import os
from urllib.parse import quote

from alembic import context
from sqlalchemy import engine_from_config, pool


config = context.config


def database_url() -> str:
    user = quote(os.getenv("PGUSER", "n8n"), safe="")
    password = quote(os.getenv("PGPASSWORD", ""), safe="")
    host = os.getenv("PGHOST", "postgres")
    port = os.getenv("PGPORT", "5432")
    database = quote(os.getenv("PGDATABASE", "n8n"), safe="")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{database}"


# ConfigParser reserves percent signs for interpolation.  Password URL encoding
# legitimately contains `%`, so escape it only at the config boundary.
config.set_main_option("sqlalchemy.url", database_url().replace("%", "%%"))
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), dialect_opts={"paramstyle": "named"},
                      version_table_schema="quant", literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, version_table_schema="quant")
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
