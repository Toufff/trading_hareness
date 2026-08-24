import sys
import unittest

import entrypoint


class EntrypointTests(unittest.TestCase):
    def test_migration_command_uses_the_active_python_environment(self):
        self.assertEqual(
            entrypoint.migration_command(),
            [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        )


if __name__ == "__main__":
    unittest.main()
