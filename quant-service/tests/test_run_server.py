import sys
import unittest

import run_server


class RunServerTests(unittest.TestCase):
    def test_configures_psycopg_compatible_loop_on_windows(self):
        result = run_server.configure_event_loop()
        self.assertEqual(result, "windows-selector" if sys.platform == "win32" else "platform-default")


if __name__ == "__main__":
    unittest.main()
