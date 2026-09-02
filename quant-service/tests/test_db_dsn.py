"""Tests for the shared n8n-instance PostgreSQL connection defaults."""

from __future__ import annotations

import unittest

from app.db_dsn import connection_params, sqlalchemy_url


class ConnectionParamsTests(unittest.TestCase):
    def test_defaults_match_the_shared_n8n_instance(self):
        params = connection_params({})
        self.assertEqual(params, {
            "host": "postgres", "port": "5432", "dbname": "n8n", "user": "n8n", "password": "",
        })

    def test_explicit_environment_overrides_every_field(self):
        params = connection_params({
            "PGHOST": "db.internal", "PGPORT": "6543", "PGDATABASE": "quant",
            "PGUSER": "quant_service", "PGPASSWORD": "s3cret",
        })
        self.assertEqual(params, {
            "host": "db.internal", "port": "6543", "dbname": "quant",
            "user": "quant_service", "password": "s3cret",
        })


class SqlalchemyUrlTests(unittest.TestCase):
    def test_builds_a_psycopg_url_from_defaults(self):
        self.assertEqual(sqlalchemy_url({}), "postgresql+psycopg://n8n:@postgres:5432/n8n")

    def test_url_encodes_special_characters_in_credentials(self):
        url = sqlalchemy_url({"PGUSER": "a user", "PGPASSWORD": "p@ss/word"})
        self.assertIn("a%20user", url)
        self.assertIn("p%40ss%2Fword", url)


if __name__ == "__main__":
    unittest.main()
