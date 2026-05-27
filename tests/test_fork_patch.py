"""Guards the Hermes fork patch against malformed hunk headers.

Regression for the 'corrupt patch at line 104' class: the PLATFORM_HINTS
hunk gained reply-quote + self-status doc content over time, but its
`@@ ... +60 @@` header line count was never updated to match the body,
so `git apply` / `git am` failed on every fresh install. `git apply
--numstat` parses the patch format (without needing the Hermes target
files present) and exits non-zero if any hunk header is inconsistent —
exactly the check that should have caught this before shipping.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PATCH = REPO_ROOT / "hermes-fork-patch" / "0001-bgos-integration.patch"


def test_fork_patch_exists():
    assert PATCH.is_file(), f"fork patch missing at {PATCH}"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_fork_patch_is_well_formed():
    """`git apply --numstat` parses every hunk; a malformed header (counts
    not matching the body) makes it exit non-zero with 'corrupt patch'."""
    result = subprocess.run(
        ["git", "apply", "--numstat", str(PATCH)],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        "fork patch is malformed (git apply --numstat failed):\n"
        f"{result.stderr}"
    )
    # Sanity: the parse should enumerate the file-diffs the patch carries.
    assert "agent/prompt_builder.py" in result.stdout
    assert "gateway/platforms/bgos.py" in result.stdout
