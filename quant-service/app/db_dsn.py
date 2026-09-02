"""Shared PostgreSQL connection defaults for the n8n-provisioned instance.

Every deployment of this service points at the same n8n-provisioned
PostgreSQL instance and reuses its default database/role names.  This module
is the single place that default lives, so ``app/database.py`` (runtime
pools), ``entrypoint.py`` (pre-boot migration lock) and
``migrations/env.py`` (Alembic CLI) cannot drift out of sync with each other.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import quote

DEFAULT_PGHOST = "postgres"
DEFAULT_PGPORT = "5432"
DEFAULT_PGDATABASE = "n8n"
DEFAULT_PGUSER = "n8n"
DEFAULT_PGPASSWORD = ""


def connection_params(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the raw host/port/dbname/user/password strings, unencoded."""
    env = os.environ if environ is None else environ
    return {
        "host": env.get("PGHOST", DEFAULT_PGHOST),
        "port": env.get("PGPORT", DEFAULT_PGPORT),
        "dbname": env.get("PGDATABASE", DEFAULT_PGDATABASE),
        "user": env.get("PGUSER", DEFAULT_PGUSER),
        "password": env.get("PGPASSWORD", DEFAULT_PGPASSWORD),
    }


def sqlalchemy_url(environ: Mapping[str, str] | None = None) -> str:
    """Build the ``postgresql+psycopg://`` URL Alembic's ``env.py`` needs.

    ConfigParser reserves percent signs for interpolation, so any literal
    ``%`` a caller gets back from this (via URL-encoding a password) must
    still be doubled at the ``alembic`` config boundary -- that escaping
    stays the caller's responsibility, same as before this module existed.
    """
    params = connection_params(environ)
    user = quote(params["user"], safe="")
    password = quote(params["password"], safe="")
    database = quote(params["dbname"], safe="")
    return f"postgresql+psycopg://{user}:{password}@{params['host']}:{params['port']}/{database}"


__all__ = [
    "DEFAULT_PGDATABASE",
    "DEFAULT_PGHOST",
    "DEFAULT_PGPASSWORD",
    "DEFAULT_PGPORT",
    "DEFAULT_PGUSER",
    "connection_params",
    "sqlalchemy_url",
]
