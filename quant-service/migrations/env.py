from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from app import db_dsn


config = context.config


# ConfigParser reserves percent signs for interpolation.  Password URL encoding
# legitimately contains `%`, so escape it only at the config boundary.
config.set_main_option("sqlalchemy.url", db_dsn.sqlalchemy_url().replace("%", "%%"))
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
