"""scripts/check_hermes_imports.py: every Hermes name the plugin imports must
exist in Hermes. The weekly workflow (.github/workflows/hermes-imports.yml)
runs it against Hermes' latest release and main; these tests pin what it
finds and what it reports, on fixtures, without a Hermes checkout."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_hermes_imports.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hermes_imports"
HERMES = FIXTURES / "hermes"
PLUGIN_OK = FIXTURES / "plugin_ok" / "src"
PLUGIN_BUG = FIXTURES / "plugin_bug" / "src"


def _load():
    spec = importlib.util.spec_from_file_location("check_hermes_imports", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


chk = _load()


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, cwd=REPO
    )


def _targets(src: Path) -> set[str]:
    return {ref.target for ref in chk.collect_imports(src)}


def _src_with(tmp_path: Path, code: str) -> Path:
    pkg = tmp_path / "src" / "fixture_plugin"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text(code)
    return tmp_path / "src"


def _missing(src: Path) -> dict[str, str]:
    tree = chk.HermesTree(HERMES)
    return {m.target: m.reason for m in tree.check(chk.collect_imports(src))}


# --- red first: the known bug ------------------------------------------------


def test_reports_gateway_slash_confirm_resolve_as_missing():
    """The adapter imported gateway.slash_confirm for months; Hermes only ever
    had tools.slash_confirm, and the try/except hid it. The check names it."""
    out = _run("--hermes", str(HERMES), "--src", str(PLUGIN_BUG), "--label", "fixture")
    assert out.returncode == 1, out.stdout + out.stderr
    assert "MISSING gateway.slash_confirm.resolve" in out.stdout
    assert "neither gateway/slash_confirm.py nor gateway/slash_confirm/" in out.stdout
    assert "imported at src/fixture_plugin/adapter.py:18" in out.stdout
    # The good import in the same file is not reported.
    assert "tools.approval" not in out.stdout
    assert "1 of 2 names MISSING" in out.stdout


def test_passes_when_every_name_exists():
    out = _run("--hermes", str(HERMES), "--src", str(PLUGIN_OK), "--label", "fixture")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "MISSING" not in out.stdout
    assert "all 16 names resolve" in out.stdout


def test_the_fixed_import_resolves_where_the_old_one_did_not():
    assert _missing(PLUGIN_OK) == {}
    assert set(_missing(PLUGIN_BUG)) == {"gateway.slash_confirm.resolve"}


# --- collecting: what counts as a Hermes import ------------------------------


def test_collects_every_shape_the_plugin_uses():
    assert _targets(PLUGIN_OK) == {
        # inside try blocks, one aliased
        "tools.slash_confirm.resolve",
        "tools.approval.resolve_gateway_approval",
        "gateway.config.Platform",
        "gateway.platforms.base.BasePlatformAdapter",
        "gateway.platforms.base.MessageEvent",
        "gateway.platforms.base.MessageType",
        "gateway.platforms.base.SendResult",
        # a plain module import
        "gateway.platforms.base",
        # inside functions, an async one, a try inside a function
        "gateway.platforms.base.build_session_key",
        "gateway.platforms.base.cache_audio_from_bytes",
        "hermes_cli.commands",
        "hermes_cli.profiles.create_profile",
        "hermes_cli.profiles.normalize_profile_name",
        "hermes_cli.profiles.profile_exists",
        "hermes_constants.get_hermes_home",
        # importlib.import_module with a literal
        "tools.memory_tool",
    }


def test_skips_stdlib_third_party_relative_and_own_tests():
    targets = _targets(PLUGIN_OK)
    roots = {t.split(".", 1)[0] for t in targets}
    assert roots == {"tools", "gateway", "hermes_cli", "hermes_constants"}
    assert not any(t.startswith("tests.") for t in targets)


def test_an_unknown_root_counts_as_hermes(tmp_path):
    """A root that is neither stdlib, own nor a known dependency is checked,
    so a new Hermes top-level module can never be skipped by mistake."""
    src = _src_with(tmp_path, "import hermes_new_thing\n")
    assert _missing(src) == {
        "hermes_new_thing": "no module hermes_new_thing: neither hermes_new_thing.py nor hermes_new_thing/ exists"
    }


def test_real_plugin_source_is_read_including_try_blocks():
    """Against this repo's own src/: the slash confirm import inside its try
    block, a function-local import, and an import_module literal are found."""
    refs = chk.collect_imports(REPO / "src")
    targets = {r.target for r in refs}
    assert "tools.slash_confirm.resolve" in targets
    assert "gateway.slash_confirm.resolve" not in targets
    assert "hermes_cli.goals.GoalManager" in targets  # inside a function
    assert "hermes_state" in targets  # importlib.import_module("hermes_state")
    assert "tools.approval.resolve_gateway_approval" in targets
    assert not any(r.module.startswith("hermes_channel_bgos") for r in refs)
    assert not any(r.module.startswith("tests") for r in refs)
    slash = [r for r in refs if r.target == "tools.slash_confirm.resolve"]
    source = (REPO / slash[0].path).read_text().splitlines()
    assert "from tools.slash_confirm import resolve" in source[slash[0].line - 1]


# --- resolving: static rules -------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "from gateway.platforms.base import SendResult\n",  # star re-export, in __all__
        "from tools import slash_confirm\n",  # a submodule of a package
        "from tools.approval import resolve_gateway_approval\n",  # bound in try and except
        "from gateway.platforms.base import cache_audio_from_bytes\n",  # bound under if
        "from hermes_cli.profiles import profile_exists\n",  # tuple unpacking
        "import gateway.config\n",
        "from gateway import platforms\n",
    ],
)
def test_resolves(tmp_path, code):
    assert _missing(_src_with(tmp_path, code)) == {}


@pytest.mark.parametrize(
    "code, target, why",
    [
        (
            "from tools.slash_confirm import resolve_card\n",
            "tools.slash_confirm.resolve_card",
            "tools.slash_confirm (tools/slash_confirm.py) does not define or re-export resolve_card",
        ),
        (
            "from gateway.platforms.base import NotExported\n",
            "gateway.platforms.base.NotExported",
            "does not define or re-export NotExported",
        ),
        (
            "from gateway.platforms.base import annotated_only\n",
            "gateway.platforms.base.annotated_only",
            "does not define or re-export annotated_only",
        ),
        (
            "import gateway.platforms.telegram\n",
            "gateway.platforms.telegram",
            "gateway.platforms exists but neither gateway/platforms/telegram.py",
        ),
        (
            "from tools.slash_confirm.inner import x\n",
            "tools.slash_confirm.inner.x",
            "no module tools.slash_confirm.inner",
        ),
        (
            # Only gateway's module __getattr__ could serve it: not static.
            "from gateway import GatewayRunner\n",
            "gateway.GatewayRunner",
            "it has a module __getattr__, which a static check cannot read",
        ),
    ],
)
def test_reports_missing(tmp_path, code, target, why):
    missing = _missing(_src_with(tmp_path, code))
    assert list(missing) == [target]
    assert why in missing[target]


def test_one_entry_per_name_with_every_import_site(tmp_path):
    src = _src_with(
        tmp_path,
        "def a():\n    from gateway.slash_confirm import resolve\n"
        "def b():\n    from gateway.slash_confirm import resolve\n",
    )
    tree = chk.HermesTree(HERMES)
    [entry] = tree.check(chk.collect_imports(src))
    assert entry.target == "gateway.slash_confirm.resolve"
    assert [r.line for r in entry.refs] == [2, 4]


# --- command line ------------------------------------------------------------


def test_finding_no_hermes_imports_is_an_error_not_a_pass(tmp_path):
    src = _src_with(tmp_path, "import json\n")
    out = _run("--hermes", str(HERMES), "--src", str(src))
    assert out.returncode == 2
    assert "found no Hermes imports" in out.stderr


def test_a_folder_that_is_not_hermes_is_an_error(tmp_path):
    out = _run("--hermes", str(tmp_path), "--src", str(PLUGIN_OK))
    assert out.returncode == 2
    assert "not a hermes-agent checkout" in out.stderr


def test_a_source_file_that_does_not_parse_is_an_error(tmp_path):
    src = _src_with(tmp_path, "def broken(:\n")
    out = _run("--hermes", str(HERMES), "--src", str(src))
    assert out.returncode == 2
    assert "cannot parse" in out.stderr


def test_summary_lists_each_missing_name(tmp_path):
    summary = tmp_path / "summary.md"
    out = _run(
        "--hermes", str(HERMES), "--src", str(PLUGIN_BUG),
        "--label", "main abc123", "--summary", str(summary),
    )
    assert out.returncode == 1
    text = summary.read_text()
    assert "### Hermes imports against main abc123" in text
    assert "| `gateway.slash_confirm.resolve` |" in text
    assert "adapter.py:18" in text


def test_summary_on_a_pass(tmp_path):
    summary = tmp_path / "summary.md"
    out = _run("--hermes", str(HERMES), "--src", str(PLUGIN_OK), "--summary", str(summary))
    assert out.returncode == 0
    assert "All 16 Hermes names the plugin imports resolve" in summary.read_text()


def test_the_workflow_runs_the_script_against_release_and_main():
    workflow = (REPO / ".github" / "workflows" / "hermes-imports.yml").read_text()
    assert "scripts/check_hermes_imports.py" in workflow
    assert "schedule:" in workflow and "workflow_dispatch:" in workflow
    assert "releases/latest" in workflow
    assert "- release" in workflow and "- main" in workflow
    # Same runners as the repo's other workflows.
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    assert "runs-on: ubuntu-latest" in ci
    assert "runs-on: ubuntu-latest" in workflow
