from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_repository_documents_commit_and_live_acceptance_boundaries():
    guide = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    architecture_words = " ".join(architecture.split())

    assert "Commit each coherent, tested change separately" in guide
    assert "staged secret scan" in guide
    assert "Never describe a source-only or mocked test as an end-to-end acceptance" in guide
    assert "Production normally publishes only a clean checkout" in architecture_words


def test_release_rejects_dirty_checkout_by_default_and_keeps_tests_enabled():
    script = (ROOT / "scripts" / "windows" / "publish-stock-release.ps1").read_text(encoding="utf-8")

    assert "if ($dirty -and -not $AllowDirty)" in script
    assert "throw 'The source checkout is dirty." in script
    assert "if (-not $SkipTests)" in script
    assert "git -C $source diff --check" in script


def test_local_agent_and_browser_artifacts_are_ignored():
    patterns = set((ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())

    assert {".agents/", ".codex/", ".playwright-cli/", "frontend/test-results/"} <= patterns
