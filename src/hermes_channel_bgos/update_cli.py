"""Official self-update command for the Hermes BGOS channel.

The default invocation is a read-only dry run apart from refreshing Git refs:

    python -m hermes_channel_bgos.update

Pass ``--yes`` to apply the rendered plan. A restart command is detected and
printed, but this command never restarts the Hermes gateway itself.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Sequence

import click

from . import __version__


PACKAGE_NAME = "hermes-channel-bgos"
REPO_URL = "https://github.com/BrandGrowthOS/hermes-channel-bgos.git"
MAIN_REF = "origin/main"

_RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:[A-Za-z0-9.+-]*)?$")
_COMMIT_PIN = re.compile(r"^[0-9a-fA-F]{7,40}$")
_OFFICIAL_REMOTES = (
    re.compile(
        r"^https://github\.com/BrandGrowthOS/hermes-channel-bgos(?:\.git)?/?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^git@github\.com:BrandGrowthOS/hermes-channel-bgos(?:\.git)?$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^ssh://git@github\.com/BrandGrowthOS/hermes-channel-bgos(?:\.git)?/?$",
        re.IGNORECASE,
    ),
)


class UpdateError(RuntimeError):
    """A fatal updater error with an operator-facing message."""


class CommandFailed(UpdateError):
    """A failed subprocess whose original output must be shown verbatim."""

    def __init__(
        self,
        argv: Sequence[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        super().__init__(
            f"command exited with status {returncode}: {shlex.join(argv)}"
        )
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class InstallKind(str, Enum):
    CHECKOUT = "checkout"
    PIP = "pip"


@dataclass(frozen=True)
class InstallInfo:
    kind: InstallKind
    package_file: Path
    python: Path
    checkout_root: Path | None = None


@dataclass(frozen=True)
class TargetChoice:
    ref: str
    source: str


@dataclass(frozen=True)
class Target:
    ref: str
    commit: str
    version: str
    source: str


@dataclass(frozen=True)
class ReconResult:
    current_commit: str | None
    main_commit: str
    main_version: str
    latest_release_tag: str | None
    latest_release_commit: str | None
    latest_release_version: str | None
    target: Target
    incoming: tuple[str, ...]
    already_current: bool
    dirty: bool


@dataclass(frozen=True)
class UpdatePlan:
    install: InstallInfo
    current_version: str
    recon: ReconResult
    rollback: str
    apply_argv: tuple[str, ...]
    restart_command: str | None


def _echo_verbatim(value: str, *, err: bool = False) -> None:
    if value:
        click.echo(value, err=err, nl=not value.endswith("\n"))


def _run_command(
    argv: Sequence[str],
    *,
    announce: bool = True,
    show_output: bool = False,
) -> str:
    command = [str(arg) for arg in argv]
    if announce:
        click.echo(f"  $ {shlex.join(command)}")
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise UpdateError(str(exc)) from exc
    if result.returncode != 0:
        raise CommandFailed(
            command,
            result.returncode,
            result.stdout or "",
            result.stderr or "",
        )
    if show_output:
        _echo_verbatim(result.stdout or "")
        _echo_verbatim(result.stderr or "", err=True)
    return result.stdout.strip()


def _project_name(root: Path) -> str | None:
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    name = data.get("project", {}).get("name")
    return str(name) if name is not None else None


def find_checkout_root(package_file: Path) -> Path | None:
    """Find the editable checkout owning an imported package file."""
    resolved = package_file.resolve()
    for candidate in resolved.parents:
        if not (candidate / ".git").exists():
            continue
        if _project_name(candidate) == PACKAGE_NAME:
            return candidate
    return None


def detect_install_kind(package_file: Path) -> InstallKind:
    """Classify an import path as a site install or editable checkout.

    The site directory check intentionally comes first. A virtualenv can live
    inside this repository, and walking upward to ``.git`` first would then
    misclassify a non-editable wheel install as the checkout.
    """
    resolved = package_file.resolve()
    if any(
        parent.name in {"site-packages", "dist-packages"}
        for parent in resolved.parents
    ):
        return InstallKind.PIP
    if find_checkout_root(resolved) is not None:
        return InstallKind.CHECKOUT
    raise UpdateError(
        "unsupported install layout for imported package at " + str(resolved)
    )


def locate_install() -> InstallInfo:
    import hermes_channel_bgos

    raw_file = getattr(hermes_channel_bgos, "__file__", None)
    if not raw_file:
        raise UpdateError("hermes_channel_bgos has no import file path")
    package_file = Path(raw_file).resolve(strict=True)
    kind = detect_install_kind(package_file)
    root = find_checkout_root(package_file) if kind is InstallKind.CHECKOUT else None
    if kind is InstallKind.CHECKOUT and root is None:
        raise UpdateError("editable checkout root could not be located")
    return InstallInfo(
        kind=kind,
        package_file=package_file,
        checkout_root=root,
        # Keep the venv launcher path. Resolving its symlink would select the
        # base interpreter and make pip or fresh-process verification run in
        # the wrong environment.
        python=Path(sys.executable).expanduser().absolute(),
    )


def resolve_target(
    pin: str | None,
    latest_release_tag: str | None,
    main_ref: str = MAIN_REF,
) -> TargetChoice:
    """Choose an explicit pin, then a release tag, then main.

    Callers may suppress a stale release tag by passing ``None``. Editable
    checkouts do this for unpinned updates because their required update path
    is a fast-forward pull of main.
    """
    if pin is not None:
        return TargetChoice(pin, "explicit pin")
    if latest_release_tag is not None:
        return TargetChoice(latest_release_tag, "release tag")
    return TargetChoice(main_ref, "main")


def _release_version(tag: str) -> tuple[int, int, int] | None:
    match = _RELEASE_TAG.fullmatch(tag)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def _version_tuple(version: str) -> tuple[int, int, int] | None:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def _tag_is_not_older(tag: str, current_version: str) -> bool:
    tag_version = _release_version(tag)
    current = _version_tuple(current_version)
    return tag_version is not None and current is not None and tag_version >= current


def _release_is_eligible(
    tag: str,
    tag_project_version: str,
    current_version: str,
) -> bool:
    """Accept a release only when its name matches its packaged version."""
    return (
        tag == f"v{tag_project_version}"
        and _tag_is_not_older(tag, current_version)
    )


def _is_official_remote(url: str) -> bool:
    return any(pattern.fullmatch(url.strip()) for pattern in _OFFICIAL_REMOTES)


def _redact_remote(url: str) -> str:
    return re.sub(r"(?<=://)[^/@\s]+@", "<redacted>@", url)


def _read_version_at(root: Path, commit: str) -> str:
    text = _run_command(
        ["git", "-C", str(root), "show", f"{commit}:pyproject.toml"]
    )
    try:
        version = tomllib.loads(text)["project"]["version"]
    except (KeyError, TypeError, ValueError) as exc:
        raise UpdateError(
            f"could not read project.version at {commit}: {exc}"
        ) from exc
    if not isinstance(version, str) or not version.strip():
        raise UpdateError(f"invalid project.version at {commit}: {version!r}")
    return version.strip()


def _git_commit(root: Path, ref: str) -> str:
    commit = _run_command(
        ["git", "-C", str(root), "rev-parse", "--verify", f"{ref}^{{commit}}"]
    )
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
        raise UpdateError(f"git returned an invalid commit for {ref}: {commit!r}")
    return commit.lower()


def _all_tags(root: Path) -> list[str]:
    output = _run_command([
        "git", "-C", str(root), "for-each-ref",
        "--sort=-version:refname", "--format=%(refname:short)", "refs/tags",
    ])
    return [line.strip() for line in output.splitlines() if line.strip()]


def _latest_release_tag(tags: Sequence[str]) -> str | None:
    return next((tag for tag in tags if _RELEASE_TAG.fullmatch(tag)), None)


def _resolve_choice_commit(
    root: Path,
    choice: TargetChoice,
    all_tags: Sequence[str],
) -> str:
    if choice.source == "main":
        return _git_commit(root, MAIN_REF)
    if choice.ref in all_tags:
        return _git_commit(root, f"refs/tags/{choice.ref}")
    if choice.source == "explicit pin" and _COMMIT_PIN.fullmatch(choice.ref):
        return _git_commit(root, choice.ref)
    raise UpdateError(
        f"unknown pin {choice.ref!r}; use an existing tag or a 7 to 40 character SHA"
    )


def _installed_vcs_commit() -> str | None:
    try:
        distribution = importlib.metadata.distribution(PACKAGE_NAME)
        raw = distribution.read_text("direct_url.json")
        data = json.loads(raw) if raw else {}
    except (importlib.metadata.PackageNotFoundError, ValueError, TypeError):
        return None
    commit = data.get("vcs_info", {}).get("commit_id")
    if isinstance(commit, str) and re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
        return commit.lower()
    return None


def _prepare_remote_checkout() -> tuple[tempfile.TemporaryDirectory[str], Path]:
    temp = tempfile.TemporaryDirectory(prefix="hermes-bgos-update-")
    root = Path(temp.name)
    try:
        _run_command(["git", "init", "--quiet", str(root)])
        _run_command(["git", "-C", str(root), "remote", "add", "origin", REPO_URL])
    except Exception:
        temp.cleanup()
        raise
    return temp, root


def git_recon(
    install: InstallInfo,
    *,
    current_version: str,
    pin: str | None,
) -> ReconResult:
    """Fetch official refs and build the immutable update target."""
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if install.kind is InstallKind.CHECKOUT:
        if install.checkout_root is None:
            raise UpdateError("checkout install has no checkout root")
        root = install.checkout_root
        remote = _run_command(
            ["git", "-C", str(root), "remote", "get-url", "origin"]
        )
        if not _is_official_remote(remote):
            raise UpdateError(
                f"origin is not the official {PACKAGE_NAME} repository: "
                f"{_redact_remote(remote)}"
            )
        current_commit = _git_commit(root, "HEAD")
    else:
        temporary, root = _prepare_remote_checkout()
        current_commit = _installed_vcs_commit()

    try:
        _run_command([
            "git", "-C", str(root), "fetch", "--prune", "--tags", "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        ], show_output=True)
        main_commit = _git_commit(root, MAIN_REF)
        main_version = _read_version_at(root, main_commit)
        tags = _all_tags(root)
        latest_tag = _latest_release_tag(tags)
        latest_commit = (
            _git_commit(root, f"refs/tags/{latest_tag}") if latest_tag else None
        )
        latest_version = (
            _read_version_at(root, latest_commit) if latest_commit else None
        )
        if (
            latest_tag is not None
            and latest_version is not None
            and latest_tag != f"v{latest_version}"
        ):
            raise UpdateError(
                f"release tag {latest_tag} contains project version "
                f"{latest_version}; refusing an inconsistent release"
            )

        release_candidate: str | None = None
        if (
            install.kind is InstallKind.PIP
            and latest_tag is not None
            and latest_version is not None
        ):
            if _release_is_eligible(
                latest_tag,
                latest_version,
                current_version,
            ):
                release_candidate = latest_tag
        choice = resolve_target(
            pin,
            release_candidate if install.kind is InstallKind.PIP else None,
        )
        target_commit = _resolve_choice_commit(root, choice, tags)
        target_version = _read_version_at(root, target_commit)

        shortlog_base = current_commit
        if install.kind is InstallKind.PIP and shortlog_base is not None:
            reachable = set(_run_command([
                "git", "-C", str(root), "rev-list", "--all",
            ]).splitlines())
            if shortlog_base not in reachable:
                shortlog_base = None
        if shortlog_base is None:
            current_tag = f"v{current_version}"
            if current_tag in tags:
                shortlog_base = _git_commit(root, f"refs/tags/{current_tag}")
        if shortlog_base is None:
            incoming = (
                "unavailable (installed package has no Git commit metadata)",
            )
        else:
            output = _run_command([
                "git", "-C", str(root), "log", "--oneline", "--no-decorate",
                f"{shortlog_base}..{target_commit}",
            ])
            incoming = tuple(output.splitlines())

        if install.kind is InstallKind.CHECKOUT:
            already_current = (
                current_commit == target_commit
                and current_version == target_version
            )
            dirty = bool(_run_command([
                "git", "-C", str(root), "status", "--porcelain",
                "--untracked-files=normal",
            ]))
        else:
            if current_commit is not None:
                already_current = (
                    current_commit == target_commit
                    and current_version == target_version
                )
            else:
                already_current = current_version == target_version
            dirty = False

        return ReconResult(
            current_commit=current_commit,
            main_commit=main_commit,
            main_version=main_version,
            latest_release_tag=latest_tag,
            latest_release_commit=latest_commit,
            latest_release_version=latest_version,
            target=Target(
                ref=choice.ref,
                commit=target_commit,
                version=target_version,
                source=choice.source,
            ),
            incoming=incoming,
            already_current=already_current,
            dirty=dirty,
        )
    finally:
        if temporary is not None:
            temporary.cleanup()


def frozen_requirement(freeze_output: str) -> str:
    """Return the exact frozen requirement for this distribution."""
    matches: list[str] = []
    for raw_line in freeze_output.splitlines():
        line = raw_line.strip()
        lowered = line.lower().replace("_", "-")
        if lowered.startswith(PACKAGE_NAME + "=="):
            matches.append(line)
        elif lowered.startswith(PACKAGE_NAME + " @ "):
            matches.append(line)
        elif line.startswith("-e ") and re.search(
            r"#egg=hermes(?:-|_)channel(?:-|_)bgos(?:&|$)", line, re.IGNORECASE,
        ):
            matches.append(line)
    if len(matches) != 1:
        raise UpdateError(
            "pip freeze did not contain exactly one hermes-channel-bgos requirement"
        )
    return matches[0]


def _pip_requirement_args(requirement: str) -> list[str]:
    if requirement.startswith("-e "):
        parts = shlex.split(requirement)
        if len(parts) != 2 or parts[0] != "-e":
            raise UpdateError(f"unsupported editable freeze requirement: {requirement}")
        return parts
    return [requirement]


def rollback_command(
    kind: InstallKind,
    *,
    python: Path,
    checkout_root: Path | None = None,
    current_commit: str | None = None,
    requirement: str | None = None,
) -> str:
    """Derive the exact operator-run rollback command for a snapshot."""
    if kind is InstallKind.CHECKOUT:
        if checkout_root is None or current_commit is None:
            raise UpdateError("checkout rollback requires a root and current commit")
        argv = [
            "git", "-C", str(checkout_root), "checkout", "--detach", current_commit,
        ]
    else:
        if requirement is None:
            raise UpdateError("pip rollback requires a frozen requirement")
        argv = [
            str(python), "-m", "pip", "install", "--force-reinstall",
            *_pip_requirement_args(requirement),
        ]
    return shlex.join(argv)


def _snapshot(install: InstallInfo, recon: ReconResult) -> str:
    if install.kind is InstallKind.CHECKOUT:
        return rollback_command(
            install.kind,
            python=install.python,
            checkout_root=install.checkout_root,
            current_commit=recon.current_commit,
        )
    output = _run_command([
        str(install.python), "-m", "pip", "freeze", "--all",
    ])
    requirement = frozen_requirement(output)
    click.echo(f"  Frozen requirement: {requirement}")
    return rollback_command(
        install.kind,
        python=install.python,
        requirement=requirement,
    )


def _apply_argv(install: InstallInfo, recon: ReconResult, pin: str | None) -> tuple[str, ...]:
    if install.kind is InstallKind.CHECKOUT:
        if install.checkout_root is None:
            raise UpdateError("checkout install has no checkout root")
        if pin is None:
            return (
                "git", "-C", str(install.checkout_root),
                "pull", "--ff-only", "origin", recon.target.commit,
            )
        return (
            "git", "-C", str(install.checkout_root),
            "checkout", "--detach", recon.target.commit,
        )
    return (
        str(install.python), "-m", "pip", "install", "--upgrade",
        f"git+{REPO_URL}@{recon.target.commit}",
    )


def _probe(argv: Sequence[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            [str(arg) for arg in argv],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None


def detect_restart_command() -> str | None:
    """Detect the supported gateway supervisor without changing its state."""
    uid = os.getuid() if hasattr(os, "getuid") else None
    if uid is not None and shutil.which("launchctl"):
        label = f"gui/{uid}/ai.hermes.gateway"
        result = _probe(["launchctl", "print", label])
        if result is not None and result.returncode == 0:
            return f"launchctl kickstart -k {label}"

    if shutil.which("systemctl"):
        result = _probe([
            "systemctl", "--user", "show", "-p", "LoadState", "--value",
            "hermes-gateway.service",
        ])
        loaded = (
            result is not None
            and result.returncode == 0
            and result.stdout.strip() == "loaded"
        )
        if loaded:
            return "systemctl --user restart hermes-gateway.service"
    return None


def render_plan(plan: UpdatePlan) -> str:
    """Render the complete apply and rollback plan without side effects."""
    recon = plan.recon
    lines = [
        "PLAN",
        f"  Install kind: {plan.install.kind.value}",
        f"  Current version: {plan.current_version}",
    ]
    if recon.current_commit:
        lines.append(f"  Current commit: {recon.current_commit}")
    lines.extend([
        f"  Target: {recon.target.ref} ({recon.target.source})",
        f"  Target commit: {recon.target.commit}",
        f"  Target version: {recon.target.version}",
        "  Incoming commits:",
    ])
    if recon.incoming:
        lines.extend(f"    {line}" for line in recon.incoming)
    else:
        lines.append("    (none)")
    lines.extend([
        f"  Already current: {'yes' if recon.already_current else 'no'}",
        f"  Dirty checkout: {'yes' if recon.dirty else 'no'}",
        f"  Apply command: {shlex.join(plan.apply_argv)}",
        f"  Rollback command: {plan.rollback}",
    ])
    if plan.restart_command:
        lines.append(f"  Restart command: {plan.restart_command}")
    else:
        lines.append("  Restart command: no launchd or systemd user unit detected")
    return "\n".join(lines)


def _print_recon(recon: ReconResult, current_version: str) -> None:
    click.echo(f"  Current version: {current_version}")
    if recon.current_commit:
        click.echo(f"  Current commit: {recon.current_commit}")
    else:
        click.echo("  Current commit: unavailable from installed metadata")
    click.echo(
        f"  origin/main: {recon.main_commit} (version {recon.main_version})"
    )
    if recon.latest_release_tag:
        click.echo(
            "  Latest release tag: "
            f"{recon.latest_release_tag} at {recon.latest_release_commit} "
            f"(version {recon.latest_release_version})"
        )
    else:
        click.echo("  Latest release tag: none")
    click.echo(
        f"  Selected target: {recon.target.ref} at {recon.target.commit} "
        f"(version {recon.target.version})"
    )
    click.echo("  Incoming commits:")
    if recon.incoming:
        for line in recon.incoming:
            click.echo(f"    {line}")
    else:
        click.echo("    (none)")


def _verify(plan: UpdatePlan) -> str:
    code = (
        "import hermes_channel_bgos; "
        "print(hermes_channel_bgos.__version__)"
    )
    output = _run_command([str(plan.install.python), "-c", code])
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        raise UpdateError("version verification produced no output")
    version = lines[-1]
    if version != plan.recon.target.version:
        raise UpdateError(
            "verification failed: imported version "
            f"{version} does not match target {plan.recon.target.version}"
        )
    if not plan.recon.already_current and version == plan.current_version:
        raise UpdateError(
            f"verification failed: version did not change from {version}"
        )
    if plan.install.kind is InstallKind.CHECKOUT:
        if plan.install.checkout_root is None:
            raise UpdateError("checkout install has no checkout root")
        head = _git_commit(plan.install.checkout_root, "HEAD")
        if head != plan.recon.target.commit:
            raise UpdateError(
                "verification failed: checkout HEAD "
                f"{head} does not match target {plan.recon.target.commit}"
            )
    return version


def run_update(*, yes: bool, pin: str | None) -> None:
    if pin is not None:
        pin = pin.strip()
        if not pin:
            raise UpdateError("--pin cannot be empty")

    click.echo("LOCATE")
    install = locate_install()
    if install.kind is InstallKind.CHECKOUT:
        click.echo(f"  Install: editable checkout at {install.checkout_root}")
    else:
        click.echo(f"  Install: site-packages pip install at {install.package_file}")
    click.echo(f"  Python: {install.python}")

    click.echo("RECON")
    recon = git_recon(install, current_version=__version__, pin=pin)
    _print_recon(recon, __version__)

    click.echo("SNAPSHOT")
    rollback = _snapshot(install, recon)
    click.echo(f"  Rollback command: {rollback}")

    plan = UpdatePlan(
        install=install,
        current_version=__version__,
        recon=recon,
        rollback=rollback,
        apply_argv=_apply_argv(install, recon, pin),
        restart_command=detect_restart_command(),
    )
    click.echo(render_plan(plan))

    if not yes:
        click.echo("Dry run complete. Re-run with --yes to apply this plan.")
        return

    click.echo("APPLY")
    if recon.already_current:
        click.echo("  Already current; no apply command was needed.")
    else:
        if recon.dirty:
            raise UpdateError(
                "refusing to apply with a dirty checkout; preserve or commit "
                "the local changes and run the command again"
            )
        if __version__ == recon.target.version:
            raise UpdateError(
                "target commit differs but target version was not bumped; "
                "verification cannot satisfy the version-change requirement"
            )
        _run_command(plan.apply_argv, show_output=True)

    click.echo("VERIFY")
    verified_version = _verify(plan)
    click.echo(f"  Verified version: {verified_version}")

    click.echo("RESTART")
    if plan.restart_command:
        click.echo(f"  Run deliberately: {plan.restart_command}")
    else:
        click.echo(
            "  No launchd or systemd user gateway unit was detected. "
            "Restart Hermes with its normal supervisor."
        )
    click.echo("  The updater did not restart the gateway.")


@click.command("hermes-bgos-update")
@click.option(
    "--yes",
    is_flag=True,
    help="Apply the rendered update plan. The default is a dry run.",
)
@click.option(
    "--pin",
    metavar="SHA-OR-TAG",
    help="Update to an exact Git commit SHA or existing tag.",
)
def main(yes: bool, pin: str | None) -> None:
    """Recon, snapshot, update, verify, and print a restart command."""
    try:
        run_update(yes=yes, pin=pin)
    except CommandFailed as exc:
        _echo_verbatim(exc.stdout)
        _echo_verbatim(exc.stderr, err=True)
        click.echo(f"ERROR: {exc}", err=True)
        raise click.exceptions.Exit(exc.returncode or 1) from exc
    except UpdateError as exc:
        click.echo(f"ERROR: {exc}", err=True)
        raise click.exceptions.Exit(1) from exc


if __name__ == "__main__":  # pragma: no cover
    main()
