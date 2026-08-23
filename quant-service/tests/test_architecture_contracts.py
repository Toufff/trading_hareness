import unittest

from app.agent_context import repository_agent_context


class ArchitectureContractTests(unittest.TestCase):
    def test_agent_context_exposes_architecture_and_frontend_boundaries(self):
        payload = repository_agent_context()
        self.assertEqual(payload["entrypoints"]["architecture"], "docs/ARCHITECTURE.md")
        self.assertIn("frontend_transport", payload["module_map"])
        self.assertIn("architecture_check", payload["contracts"])
