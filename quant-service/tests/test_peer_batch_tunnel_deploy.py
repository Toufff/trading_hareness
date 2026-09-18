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

import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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

    def deployed_entrypoint(self):
        """The peer entrypoint exactly as a completed deploy leaves it."""
        return deploy.patch_entrypoint(FIXTURE.read_text(), self.known["batch_bind_address"],
                                       self.known["entrypoint_anchor"])

    def test_a_finished_deploy_exits_zero_even_though_its_entrypoint_is_unknown(self):
        # The idempotent re-run - the natural thing to do after a partial
        # failure - must not be indistinguishable from a refusal. A deployed
        # peer no longer matches any KNOWN_PEER_STATES hash, so this is judged
        # AFTER the lookup, on the evidence a real deploy leaves behind.
        (self.root / "ssh-tunnel-entrypoint.sh").write_text(self.deployed_entrypoint())
        service = rendered_service(self.known)
        service["environment"]["PEER_BATCH_DB_PORT"] = "5433"
        with self.assertRaises(SystemExit) as raised:
            self.inspect(service=service)
        self.assertEqual(raised.exception.code, 0)

    def test_a_declared_but_empty_batch_port_is_not_a_deploy(self):
        # `PEER_BATCH_DB_PORT: ${PEER_BATCH_DB_PORT:-}` in this repository's own
        # compose renders the KEY with an empty value on a peer where nothing
        # has been deployed. The old short-circuit tested for the key and
        # reported that tree as a successful no-op deploy.
        (self.root / "ssh-tunnel-entrypoint.sh").write_text(self.deployed_entrypoint())
        service = rendered_service(self.known)
        service["environment"]["PEER_BATCH_DB_PORT"] = ""
        with self.assertRaises(SystemExit) as raised:
            self.inspect(service=service)
        self.assertNotEqual(raised.exception.code, 0)
        self.assertIn("refusing to touch the peer", str(raised.exception))

    def test_a_run_interrupted_after_the_compose_write_is_not_a_deploy(self):
        # .env and compose.yaml written, the entrypoint patch never applied:
        # the value is non-empty but the forward does not exist. The entrypoint
        # still matches the known state, so this lands in the mismatch gate.
        service = rendered_service(self.known)
        service["environment"]["PEER_BATCH_DB_PORT"] = "5433"
        with self.assertRaises(SystemExit) as raised:
            self.inspect(service=service)
        self.assertNotEqual(raised.exception.code, 0)
        self.assertIn("environment_keys", str(raised.exception))


class FakeComposeRunner:
    """Records every docker/compose call and models tag -> image -> container.

    Enough of docker's real semantics to make the rollback provable: ``build``
    retags the running reference in place (which is why the pre-build tag has to
    exist at all), ``tag`` repoints a name at whatever image another name holds,
    and ``up -d --no-build`` starts a container from whatever the reference
    points at *now*.
    """

    OLD_IMAGE = "sha256:0ld0ld0ld0ld0ld0ld0ld0ld0ld0ld0ld0ld0ld0ld0ld0ld0ld0ld0ld0ld0ld0"
    NEW_IMAGE = "sha256:new0new0new0new0new0new0new0new0new0new0new0new0new0new0new0new0"
    IMAGE_REF = "trading-hareness-peer-db-tunnel:latest"

    def __init__(self, rollback_retag_fails=False):
        self.calls = []
        self.tags = {self.IMAGE_REF: self.OLD_IMAGE}
        self.container_image = self.OLD_IMAGE
        self.rollback_retag_fails = rollback_retag_fails
        self.built = False

    # --- the two subprocess entry points the script uses directly ----------
    def run(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if argv and argv[0] == "ss":
            return subprocess.CompletedProcess(
                argv, 0, stdout="State  Recv-Q Send-Q Local Address:Port\n"
                                "LISTEN 0      128        127.0.0.1:15433\n", stderr="")
        if "tag" in argv:
            source, target = argv[argv.index("tag") + 1:argv.index("tag") + 3]
            # The rollback retag is the one that restores the reference; that is
            # the call whose silent failure used to be invisible.
            if self.rollback_retag_fails and target == self.IMAGE_REF:
                return subprocess.CompletedProcess(argv, 1, stdout="",
                                                   stderr="Error response from daemon: no such image")
            self.tags[target] = self.tags.get(source, source)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if "up" in argv:
            self.container_image = self.tags[self.IMAGE_REF]
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def check_output(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        if "inspect" in argv:
            return json.dumps([{
                "Config": {"Image": self.IMAGE_REF},
                "Image": self.container_image,
                "State": {"Status": "running", "Health": {"Status": "healthy"}},
            }])
        return ""

    # --- the helpers the script calls by name ------------------------------
    def compose_run(self, *arguments, **kwargs):
        self.calls.append(["compose_run"] + list(arguments))
        if arguments[0] == "build":
            # A build retags the reference the running container came from.
            self.tags[self.IMAGE_REF] = self.NEW_IMAGE
            self.built = True
        elif arguments[0] == "up":
            self.container_image = self.tags[self.IMAGE_REF]
        return subprocess.CompletedProcess(list(arguments), 0)

    def container_id(self, service_name):
        self.calls.append(["container_id", service_name])
        return "container-" + service_name

    def wait_healthy(self, timeout=180):
        self.calls.append(["wait_healthy"])
        return "container-" + deploy.SERVICE

    def probe_port(self, port):
        self.calls.append(["probe_port", port])
        if port == deploy.INTRADAY_LOCAL_PORT and self.container_image == self.NEW_IMAGE:
            # The failure the probe ordering exists to catch: the new image
            # broke the intraday path the whole peer depends on.
            raise AssertionError(
                "db-tunnel:%s did not reach the owner database (expected 1|55432)" % port)
        return "1|55432"

    def argv_names(self):
        """A compact trace: the helper name or the docker verb of each call."""
        trace = []
        for argv in self.calls:
            if argv[0] in ("compose_run", "container_id", "wait_healthy", "probe_port"):
                trace.append(" ".join(str(part) for part in argv[:2]))
            elif argv[0] == "ss":
                trace.append("ss")
            elif "tag" in argv:
                trace.append("docker tag " + argv[argv.index("tag") + 2])
            elif "up" in argv:
                trace.append("docker compose up")
            elif "inspect" in argv:
                trace.append("docker inspect")
            else:
                trace.append(" ".join(str(part) for part in argv[-3:]))
        return trace


class RollbackExecutionTests(unittest.TestCase):
    """``main()`` is driven to the failure that happens AFTER ``up -d``.

    The previous version of this class asserted substrings over the script's own
    source, so the rollback branch stayed an unexecuted code path while the
    round called it proven. Here the handler actually runs: the files on disk
    are compared with the backup, the docker calls are recorded in order, and
    the image the container comes back on is checked.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)
        self.root = base / "peer"
        self.root.mkdir()
        self.backups = base / "backups"
        self.here = base / "here"
        self.here.mkdir()
        self.known = deploy.KNOWN_PEER_STATES[STATE]

        self.entrypoint_before = FIXTURE.read_text()
        self.compose_before = ("services:\n  db-tunnel:\n    environment:\n"
                               + deploy.COMPOSE_ANCHOR)
        self.env_before = "PEER_SSH_USER=stockowner\nREMOTE_DB_PORT=15432\n"
        (self.root / "ssh-tunnel-entrypoint.sh").write_text(self.entrypoint_before)
        (self.root / "compose.yaml").write_text(self.compose_before)
        (self.root / ".env").write_text(self.env_before)
        (self.root / "compose.intraday-owner.yaml").write_text("services: {}\n")

    def peer_config(self):
        compose_text = (self.root / "compose.yaml").read_text()
        env = dict(line.split("=", 1) for line in (self.root / ".env").read_text().splitlines()
                   if "=" in line)
        environment = {key: "" for key in self.known["environment_keys"]}
        if "PEER_BATCH_DB_PORT" in compose_text:
            environment["PEER_BATCH_DB_PORT"] = env.get("PEER_BATCH_DB_PORT", "")
            environment["PEER_BATCH_REMOTE_PORT"] = env.get("PEER_BATCH_REMOTE_PORT", "")
        return {"services": {
            deploy.SERVICE: {
                "healthcheck": {"test": list(self.known["healthcheck_test"])},
                "environment": environment,
                "volumes": [], "ports": [], "networks": ["peer"], "restart": "unless-stopped",
            },
            deploy.VERIFIER: {"environment": {key: "" for key in (
                "PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD")}},
        }}

    def drive(self, runner):
        """Run main() with every outside edge replaced by `runner`."""
        states = dict(deploy.KNOWN_PEER_STATES, **{
            STATE: dict(self.known, compose_sha256=deploy.sha256_text(self.compose_before))})
        out, err = io.StringIO(), io.StringIO()
        with patch.object(deploy, "ROOT", self.root), \
                patch.object(deploy, "HERE", self.here), \
                patch.object(deploy, "BACKUP_ROOT", self.backups), \
                patch.object(deploy, "KNOWN_PEER_STATES", states), \
                patch.object(deploy, "os", SimpleNamespace(geteuid=lambda: 0)), \
                patch.object(deploy, "config", self.peer_config), \
                patch.object(deploy, "subprocess", runner), \
                patch.object(deploy, "compose_run", runner.compose_run), \
                patch.object(deploy, "container_id", runner.container_id), \
                patch.object(deploy, "wait_healthy", runner.wait_healthy), \
                patch.object(deploy, "probe_port", runner.probe_port), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(AssertionError) as raised:
                deploy.main()
        return raised.exception, out.getvalue(), err.getvalue()

    def backup_directory(self):
        children = sorted(self.backups.iterdir())
        self.assertEqual(len(children), 1, "exactly one incident backup must be taken")
        return children[0]

    def test_the_intraday_failure_after_up_restores_files_image_and_container(self):
        runner = FakeComposeRunner()
        error, out, err = self.drive(runner)
        self.assertIn("did not reach the owner database", str(error))

        # (a) the files the script rewrote are back, byte for byte.
        backup = self.backup_directory()
        for name in deploy.NAMES:
            self.assertEqual((self.root / name).read_bytes(), (backup / name).read_bytes(),
                             "%s was not restored from the backup" % name)
        self.assertEqual((self.root / "ssh-tunnel-entrypoint.sh").read_text(),
                         self.entrypoint_before)
        self.assertEqual((self.root / "compose.yaml").read_text(), self.compose_before)
        self.assertEqual((self.root / ".env").read_text(), self.env_before)

        # (b) the script really did write before it restored - otherwise this
        #     test would pass against a handler that does nothing at all.
        self.assertTrue(runner.built, "the build must have run before the failure")
        trace = runner.argv_names()
        self.assertIn("compose_run build", trace)
        up = trace.index("compose_run up")
        self.assertLess(trace.index("compose_run build"), up, "the build precedes the recreate")
        # The failing intraday probe is the one AFTER up -d; the first is the
        # precondition baseline that proves 5432 worked before the change.
        self.assertIn("probe_port 5432", trace[:up])
        self.assertIn("probe_port 5433", trace[up:])
        self.assertIn("probe_port 5432", trace[up:])
        retag = trace.index("docker tag " + FakeComposeRunner.IMAGE_REF, up)
        recreate = trace.index("docker compose up", up)
        self.assertLess(retag, recreate, "the tag must be restored before the recreate")
        self.assertIn("probe_port 5432", trace[recreate:],
                      "5432 must be re-probed only after the container is back")

        # (c) the container is back on the image that was preserved, and both
        #     ids are printed rather than asserted behind the operator's back.
        self.assertEqual(runner.container_image, FakeComposeRunner.OLD_IMAGE)
        self.assertIn(FakeComposeRunner.OLD_IMAGE, err)
        self.assertIn("preserved image id", err)
        self.assertIn("recreated image id", err)
        self.assertIn("rolled back; intraday probe: 1|55432", err)
        self.assertNotIn("AUTOMATIC ROLLBACK FAILED", err)

    def test_a_failed_retag_is_reported_instead_of_a_rolled_back_claim(self):
        # The retag is the only thing that makes `up -d --no-build` come back on
        # the old image. If it fails, `up -d` still returns 0 - and used to be
        # reported as a successful rollback onto the REJECTED image.
        runner = FakeComposeRunner(rollback_retag_fails=True)
        error, out, err = self.drive(runner)
        self.assertIn("did not reach the owner database", str(error))
        self.assertEqual(runner.container_image, FakeComposeRunner.NEW_IMAGE,
                         "this case must genuinely leave the new image running")
        self.assertIn("AUTOMATIC ROLLBACK FAILED", err)
        self.assertIn("retag exit 1", err)
        self.assertIn(FakeComposeRunner.NEW_IMAGE, err)
        self.assertNotIn("rolled back; intraday probe", err)
        # The manual command is the operator's only way out, so it must be there.
        self.assertIn("tag", err)
        self.assertIn("pre-batch-", err)
        # The configuration files are still restored: a failed image rollback
        # must not also leave the peer's compose and entrypoint rewritten.
        for name in deploy.NAMES:
            self.assertEqual((self.root / name).read_bytes(),
                             (self.backup_directory() / name).read_bytes())


if __name__ == "__main__":
    unittest.main()
