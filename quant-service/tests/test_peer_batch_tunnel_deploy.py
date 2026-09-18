"""Behavioural tests for ``scripts/shared-peer/deploy-batch-tunnel-port.py``.

The PowerShell suite can only assert regexes over this file's source, which is
how a previous round reported ``peer_deploy_refuses_unknown_state=True`` for a
transform nobody had executed.  These tests execute the transforms instead, and
they run against ``tests/fixtures/peer-ssh-tunnel-entrypoint-nc-legacy-20260917.sh``
- a byte-identical copy of lightServer's live entrypoint (1694 bytes, SHA-256
``b0aa1e02...4072c``, fetched read-only on 2026-09-19).  If the peer's file ever
changes, the hash assertion below fails in the same breath as the deploy
script's own refusal, which is the point: the fixture and ``KNOWN_PEER_STATES``
must move together.

Nothing here touches the peer, docker, or the network: ``config()`` and ``ROOT``
are patched, so ``inspect_peer_state`` is driven entirely from in-memory data.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "shared-peer" / "deploy-batch-tunnel-port.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "peer-ssh-tunnel-entrypoint-nc-legacy-20260917.sh"
PEER_ENTRYPOINT_SHA256 = "b0aa1e02c60b7d0951e16b91250d863716ebaa554ee60ba0f18797469448072c"
STATE = "nc-legacy-20260917"


def load_module():
    spec = importlib.util.spec_from_file_location("deploy_batch_tunnel_port", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = load_module()


def rendered_service(known, **overrides):
    """The db-tunnel service as ``docker compose config`` would render it."""
    service = {
        "healthcheck": {"test": list(known["healthcheck_test"])},
        "environment": {key: "" for key in known["environment_keys"]},
    }
    if known["image"] is not None:
        service["image"] = known["image"]
    service.update(overrides)
    return service


class PeerEntrypointFixtureTests(unittest.TestCase):
    def test_fixture_is_the_hash_the_deploy_script_pins(self):
        text = FIXTURE.read_text()
        self.assertEqual(deploy.sha256_text(text), PEER_ENTRYPOINT_SHA256)
        self.assertEqual(
            deploy.KNOWN_PEER_STATES[STATE]["entrypoint_sha256"], PEER_ENTRYPOINT_SHA256,
            "the fixture and KNOWN_PEER_STATES must describe the same peer file")
        self.assertEqual(len(text.replace("\r\n", "\n").encode("utf-8")), 1694)

    def test_peer_file_binds_zero_zero_zero_zero_not_the_repo_variable(self):
        # The whole reason the script patches instead of overwriting.
        text = FIXTURE.read_text()
        self.assertIn('-L "0.0.0.0:5432:127.0.0.1:${REMOTE_DB_PORT}"', text)
        self.assertNotIn("PEER_LOCAL_BIND_ADDRESS", text)


class PatchEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.known = deploy.KNOWN_PEER_STATES[STATE]
        self.original = FIXTURE.read_text()
        self.patched = deploy.patch_entrypoint(
            self.original, self.known["batch_bind_address"], self.known["entrypoint_anchor"])

    def test_the_anchor_is_present_exactly_once_in_the_real_peer_file(self):
        self.assertEqual(self.original.count(self.known["entrypoint_anchor"]), 1)

    def test_batch_forward_uses_the_peer_own_bind_address(self):
        self.assertIn('"0.0.0.0:${PEER_BATCH_DB_PORT}:127.0.0.1:${PEER_BATCH_REMOTE_PORT:-15433}"',
                      self.patched)
        # 127.0.0.1 inside the sidecar is invisible to every sibling container:
        # the bug this script exists to avoid must not reappear as a default.
        self.assertNotIn("PEER_LOCAL_BIND_ADDRESS", self.patched)

    def test_existing_forwards_and_exec_line_are_untouched(self):
        self.assertIn('-L "0.0.0.0:5432:127.0.0.1:${REMOTE_DB_PORT}"', self.patched)
        self.assertIn('-L "0.0.0.0:5681:127.0.0.1:${REMOTE_API_PORT}"', self.patched)
        self.assertTrue(self.patched.endswith(self.original[self.original.index("exec ssh"):]))
        self.assertEqual(self.patched.count("exec ssh"), 1)

    def test_patch_is_idempotent(self):
        again = deploy.patch_entrypoint(
            self.patched, self.known["batch_bind_address"], self.known["entrypoint_anchor"])
        self.assertEqual(again, self.patched)

    def test_a_missing_anchor_refuses_instead_of_writing_a_broken_file(self):
        with self.assertRaises(AssertionError):
            deploy.patch_entrypoint(
                "#!/bin/sh\necho no anchor here\n", "0.0.0.0", self.known["entrypoint_anchor"])

    @unittest.skipUnless(shutil.which("sh"), "no POSIX sh on this host")
    def test_patched_entrypoint_is_valid_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "entrypoint.sh"
            script.write_bytes(self.patched.replace("\r\n", "\n").encode("utf-8"))
            done = subprocess.run([shutil.which("sh"), "-n", str(script)],
                                  capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)


class ComposeEnvironmentTests(unittest.TestCase):
    ANCHOR = deploy.COMPOSE_ANCHOR

    def test_inserts_the_batch_variables_once_after_the_anchor(self):
        text = "services:\n  db-tunnel:\n    environment:\n" + self.ANCHOR
        patched = deploy.add_compose_environment(text, with_local_bind=False)
        self.assertIn("PEER_BATCH_DB_PORT", patched)
        self.assertIn("PEER_BATCH_REMOTE_PORT", patched)
        self.assertNotIn("PEER_LOCAL_BIND_ADDRESS", patched)
        self.assertEqual(deploy.add_compose_environment(patched, with_local_bind=False), patched)

    def test_repo_copy_strategy_also_supplies_the_bind_address(self):
        text = "services:\n  db-tunnel:\n    environment:\n" + self.ANCHOR
        patched = deploy.add_compose_environment(text, with_local_bind=True)
        self.assertIn('PEER_LOCAL_BIND_ADDRESS: "0.0.0.0"', patched)


class InspectPeerStateTests(unittest.TestCase):
    """Drives the refusal gate with in-memory peer files - no docker, no peer."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name in deploy.NAMES + ["compose.intraday-owner.yaml"]:
            (self.root / name).write_text("placeholder\n")
        (self.root / "ssh-tunnel-entrypoint.sh").write_text(FIXTURE.read_text())
        self.known = deploy.KNOWN_PEER_STATES[STATE]
        self.compose = ("services:\n  db-tunnel:\n    environment:\n" + deploy.COMPOSE_ANCHOR)
        self.write_compose(self.compose)

    def write_compose(self, text):
        (self.root / "compose.yaml").write_text(text)
        # Pin whatever this test decided the peer's compose is, so the compose
        # hash is never the reason a case fails for an unrelated reason.
        self.compose_sha256 = deploy.sha256_text(text)

    def inspect(self, service=None, known=None, compose_sha256=None):
        known = known or self.known
        service = service if service is not None else rendered_service(known)
        patched_state = dict(known, compose_sha256=compose_sha256 or self.compose_sha256)
        states = dict(deploy.KNOWN_PEER_STATES, **{STATE: patched_state})
        with patch.object(deploy, "ROOT", self.root), \
                patch.object(deploy, "KNOWN_PEER_STATES", states), \
                patch.object(deploy, "config", lambda: {"services": {deploy.SERVICE: service}}):
            return deploy.inspect_peer_state()

    def test_the_real_peer_state_is_accepted_and_names_its_strategy(self):
        name, known = self.inspect()
        self.assertEqual(name, STATE)
        self.assertEqual(known["strategy"], "patch")
        self.assertTrue(known["observed"])

    def test_an_unknown_entrypoint_is_refused_with_the_observed_state_printed(self):
        (self.root / "ssh-tunnel-entrypoint.sh").write_text("#!/bin/sh\nexec ssh drifted\n")
        with self.assertRaises(SystemExit) as raised:
            self.inspect()
        message = str(raised.exception)
        self.assertIn("refusing to touch the peer", message)
        self.assertIn("supported entrypoint hashes", message)
        self.assertNotEqual(raised.exception.code, 0)

    def test_a_reformatted_compose_is_refused_before_any_write(self):
        # Renders identically, anchors differently: exactly the drift that used
        # to pass the gate and then die mid-write on a bare AssertionError.
        self.write_compose('services:\n  db-tunnel:\n    environment:\n'
                           '      REMOTE_API_PORT: "15681"\n')
        with self.assertRaises(SystemExit) as raised:
            self.inspect(compose_sha256=self.known["compose_sha256"])
        self.assertIn("compose_sha256", str(raised.exception))

    def test_a_drifted_healthcheck_is_refused_even_when_the_entrypoint_matches(self):
        service = rendered_service(self.known)
        service["healthcheck"] = {"test": ["CMD", "/usr/local/bin/ssh-tunnel-healthcheck"]}
        with self.assertRaises(SystemExit) as raised:
            self.inspect(service=service)
        self.assertIn("healthcheck_test", str(raised.exception))

    def test_a_drifted_image_is_refused(self):
        service = rendered_service(self.known, image="somebody-elses:latest")
        with self.assertRaises(SystemExit) as raised:
            self.inspect(service=service)
        self.assertIn("image", str(raised.exception))

    def test_already_deployed_exits_zero(self):
        # The idempotent re-run - the natural thing to do after a partial
        # failure - must not be indistinguishable from a refusal.
        known = dict(self.known,
                     environment_keys=sorted(self.known["environment_keys"] + ["PEER_BATCH_DB_PORT"]))
        with self.assertRaises(SystemExit) as raised:
            self.inspect(service=rendered_service(known), known=known)
        self.assertEqual(raised.exception.code, 0)


class RollbackContractTests(unittest.TestCase):
    """The rollback must recreate the container, not just move a tag."""

    def test_the_failure_path_recreates_when_the_container_was_recreated(self):
        source = SCRIPT.read_text()
        body = source[source.index("def main("):]
        self.assertIn("recreated = True", body)
        recreate_flag = body.index("recreated = True")
        up = body.index("compose_run('up', '-d', '--no-deps', '--no-build', SERVICE)")
        self.assertLess(up, recreate_flag, "the flag must be set only after up -d succeeded")
        handler = body[body.index("except BaseException:"):]
        self.assertIn("if recreated:", handler)
        self.assertIn("['up', '-d', '--no-deps', '--no-build', SERVICE]", handler)
        self.assertLess(handler.index("if recreated:"),
                        handler.index("probe_port(INTRADAY_LOCAL_PORT)"),
                        "5432 must be re-probed only after the container is back")


if __name__ == "__main__":
    unittest.main()
