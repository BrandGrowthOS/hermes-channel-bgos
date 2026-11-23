#!/usr/bin/env python3
"""Check that every Hermes name this plugin imports still exists in Hermes.

The plugin wraps every Hermes import in ``try/except ImportError`` so that it
loads without Hermes installed. The cost is that a Hermes rename fails
silently: the adapter falls back to a stub and nobody notices (the slash
confirm resolver imported ``gateway.slash_confirm`` for months, a module
Hermes never had). This script makes that loud.

It reads the plugin source with ``ast`` (every ``from M import N``,
``import M`` and ``importlib.import_module("M")`` anywhere in a file,
including inside ``try`` blocks and function bodies), keeps the ones that name
a Hermes module, and checks each against a checkout of NousResearch/
hermes-agent, statically: the module file exists, and it defines or
re-exports the name (or the name is a submodule of the package). Hermes is
never installed or imported.

A module counts as Hermes unless its top-level name is the standard library,
this plugin's own package or tests, or a known third-party dependency
(``THIRD_PARTY`` below). So a new Hermes import can never be skipped by
mistake; a new third-party dependency shows up as a missing module until it
is added to ``THIRD_PARTY``.

Usage:
    python scripts/check_hermes_imports.py --hermes ../hermes-agent [--src src]
        [--label "v0.23.3"] [--summary "$GITHUB_STEP_SUMMARY"] [--list]

Exit codes: 0 every name resolves, 1 a module or name is missing, 2 a usage
error (no Hermes checkout, a file that does not parse, or no Hermes imports
found at all, so the check can never pass by finding nothing).

Standard library only, Python 3.11 or later.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Import roots that are dependencies, not Hermes (import names, not
# distribution names: python-socketio is ``socketio``, PyYAML is ``yaml``).
THIRD_PARTY = frozenset(
    {
        "aiohttp",
        "click",
        "engineio",
        "httpx",
        "pytest",
        "pytest_asyncio",
        "socketio",
        "yaml",
    }
)

# The plugin's own test package: ``tests.mocks.mock_hermes`` is imported as a
# fallback when Hermes is absent. Hermes has a ``tests`` folder too, but the
# plugin never imports from it.
OWN_EXTRA = frozenset({"tests"})


@dataclass(frozen=True)
class ImportRef:
    """One Hermes import found in the plugin source."""

    module: str
    name: str | None  # None for ``import M`` and ``import_module("M")``
    path: str
    line: int

    @property
    def target(self) -> str:
        return f"{self.module}.{self.name}" if self.name else self.module


@dataclass
class Missing:
    target: str
    reason: str
    refs: list[ImportRef] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Collecting imports from the plugin source
# ---------------------------------------------------------------------------


def _own_roots(src: Path) -> set[str]:
    roots = set(OWN_EXTRA)
    for child in src.iterdir():
        if child.is_dir() and (child / "__init__.py").is_file():
            roots.add(child.name)
        elif child.suffix == ".py":
            roots.add(child.stem)
    return roots


def is_hermes_module(module: str, own: set[str]) -> bool:
    root = module.split(".", 1)[0]
    if root in sys.stdlib_module_names or root in own or root in THIRD_PARTY:
        return False
    return True


def _import_module_literal(node: ast.Call) -> str | None:
    """``importlib.import_module("x.y")`` or ``import_module("x.y")``."""
    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr != "import_module":
            return None
        if not (isinstance(func.value, ast.Name) and func.value.id == "importlib"):
            return None
    elif isinstance(func, ast.Name):
        if func.id != "import_module":
            return None
    else:
        return None
    if not node.args:
        return None
    arg = node.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        if arg.value and not arg.value.startswith("."):
            return arg.value
    return None


def collect_imports(src: Path, display_root: Path | None = None) -> list[ImportRef]:
    """Every Hermes import in ``src``, in file then line order."""
    src = src.resolve()
    display_root = (display_root or src.parent).resolve()
    own = _own_roots(src)
    refs: list[ImportRef] = []
    for file in sorted(src.rglob("*.py")):
        text = file.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(file))  # SyntaxError propagates
        try:
            shown = str(file.relative_to(display_root))
        except ValueError:
            shown = str(file)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level or not node.module:
                    continue  # relative: the plugin's own package
                if not is_hermes_module(node.module, own):
                    continue
                for alias in node.names:
                    name = None if alias.name == "*" else alias.name
                    refs.append(ImportRef(node.module, name, shown, node.lineno))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if is_hermes_module(alias.name, own):
                        refs.append(ImportRef(alias.name, None, shown, node.lineno))
            elif isinstance(node, ast.Call):
                module = _import_module_literal(node)
                if module and is_hermes_module(module, own):
                    refs.append(ImportRef(module, None, shown, node.lineno))
    refs.sort(key=lambda r: (r.path, r.line, r.target))
    return refs


# ---------------------------------------------------------------------------
# Resolving against a Hermes checkout (static)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModuleInfo:
    dotted: str
    path: Path | None  # None for a namespace package (a folder, no __init__)
    is_package: bool


@dataclass
class _Scope:
    names: set[str]
    stars: list[str]  # absolute dotted modules star-imported at module level
    all_names: list[str] | None
    has_getattr: bool


class HermesTree:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self._scopes: dict[Path, _Scope] = {}

    def find_module(self, dotted: str) -> ModuleInfo | None:
        """Mirror the import system's order: a package folder with an
        ``__init__.py``, then ``name.py``, then a namespace folder."""
        base = self.root
        parts = dotted.split(".")
        for i, part in enumerate(parts):
            folder = base / part
            last = i == len(parts) - 1
            init = folder / "__init__.py"
            if init.is_file():
                if last:
                    return ModuleInfo(dotted, init, True)
                base = folder
                continue
            module_file = base / f"{part}.py"
            if module_file.is_file():
                if last:
                    return ModuleInfo(dotted, module_file, False)
                return None  # a plain module has no submodules
            if folder.is_dir():
                if last:
                    return ModuleInfo(dotted, None, True)
                base = folder
                continue
            return None
        return None

    def explain_missing_module(self, dotted: str) -> str:
        parts = dotted.split(".")
        found = []
        for i in range(1, len(parts) + 1):
            if self.find_module(".".join(parts[:i])) is None:
                break
            found.append(parts[i - 1])
        rel = "/".join(parts)
        if found:
            parent = ".".join(found)
            return (
                f"no module {dotted}: {parent} exists but neither {rel}.py "
                f"nor {rel}/ does"
            )
        return f"no module {dotted}: neither {rel}.py nor {rel}/ exists"

    def _scope(self, path: Path, dotted: str, is_package: bool) -> _Scope:
        cached = self._scopes.get(path)
        if cached is not None:
            return cached
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        package = dotted if is_package else dotted.rpartition(".")[0]
        scope = _Scope(set(), [], None, False)
        _bind_block(tree.body, scope, package)
        self._scopes[path] = scope
        return scope

    def has_name(self, info: ModuleInfo, name: str, _seen: set[str] | None = None) -> tuple[bool, bool]:
        """(found, saw a module ``__getattr__`` on the way)."""
        seen = _seen if _seen is not None else set()
        if info.dotted in seen:
            return False, False
        seen.add(info.dotted)
        if info.is_package and self.find_module(f"{info.dotted}.{name}") is not None:
            return True, False  # ``from pkg import submodule``
        if info.path is None:
            return False, False  # namespace package: submodules only
        scope = self._scope(info.path, info.dotted, info.is_package)
        if name in scope.names:
            return True, False
        dynamic = scope.has_getattr
        for star in scope.stars:
            star_info = self.find_module(star)
            if star_info is None or star_info.path is None:
                continue
            star_scope = self._scope(star_info.path, star_info.dotted, star_info.is_package)
            if star_scope.all_names is not None and name not in star_scope.all_names:
                continue
            if star_scope.all_names is None and name.startswith("_"):
                continue
            found, star_dynamic = self.has_name(star_info, name, seen)
            dynamic = dynamic or star_dynamic
            if found:
                return True, dynamic
        return False, dynamic

    def check(self, refs: list[ImportRef]) -> list[Missing]:
        missing: dict[str, Missing] = {}
        for ref in refs:
            reason = self._why_missing(ref)
            if reason is None:
                continue
            entry = missing.setdefault(ref.target, Missing(ref.target, reason))
            entry.refs.append(ref)
        return sorted(missing.values(), key=lambda m: m.target)

    def _why_missing(self, ref: ImportRef) -> str | None:
        info = self.find_module(ref.module)
        if info is None:
            return self.explain_missing_module(ref.module)
        if ref.name is None:
            return None
        found, dynamic = self.has_name(info, ref.name)
        if found:
            return None
        where = info.path.relative_to(self.root) if info.path else f"{ref.module.replace('.', '/')}/"
        reason = f"{ref.module} ({where}) does not define or re-export {ref.name}"
        if dynamic:
            reason += " (it has a module __getattr__, which a static check cannot read)"
        return reason


def _absolute(module: str | None, level: int, package: str) -> str | None:
    if level == 0:
        return module
    parts = package.split(".") if package else []
    if level - 1 > len(parts):
        return None
    base = parts[: len(parts) - (level - 1)]
    if module:
        base.append(module)
    return ".".join(base) or None


def _bind_target(target: ast.AST, names: set[str]) -> None:
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            _bind_target(elt, names)
    elif isinstance(target, ast.Starred):
        _bind_target(target.value, names)


def _literal_all(value: ast.AST) -> list[str] | None:
    if isinstance(value, (ast.List, ast.Tuple)):
        out = []
        for elt in value.elts:
            if not (isinstance(elt, ast.Constant) and isinstance(elt.value, str)):
                return None
            out.append(elt.value)
        return out
    return None


def _bind_block(body: list[ast.stmt], scope: _Scope, package: str) -> None:
    """Names bound at module level, through if/try/with/for/while/match
    blocks but not into function or class bodies."""
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope.names.add(stmt.name)
            if stmt.name == "__getattr__" and not isinstance(stmt, ast.ClassDef):
                scope.has_getattr = True
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                _bind_target(target, scope.names)
                if isinstance(target, ast.Name) and target.id == "__all__":
                    scope.all_names = _literal_all(stmt.value)
        elif isinstance(stmt, ast.AnnAssign):
            if stmt.value is not None:
                _bind_target(stmt.target, scope.names)
        elif isinstance(stmt, ast.AugAssign):
            _bind_target(stmt.target, scope.names)
            if isinstance(stmt.target, ast.Name) and stmt.target.id == "__all__":
                extra = _literal_all(stmt.value)
                if scope.all_names is not None and extra is not None:
                    scope.all_names = scope.all_names + extra
                else:
                    scope.all_names = None
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                scope.names.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(stmt, ast.ImportFrom):
            for alias in stmt.names:
                if alias.name == "*":
                    star = _absolute(stmt.module, stmt.level, package)
                    if star:
                        scope.stars.append(star)
                else:
                    scope.names.add(alias.asname or alias.name)
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            _bind_target(stmt.target, scope.names)
            _bind_block(stmt.body, scope, package)
            _bind_block(stmt.orelse, scope, package)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                if item.optional_vars is not None:
                    _bind_target(item.optional_vars, scope.names)
            _bind_block(stmt.body, scope, package)
        elif isinstance(stmt, (ast.If, ast.While)):
            _bind_block(stmt.body, scope, package)
            _bind_block(stmt.orelse, scope, package)
        elif isinstance(stmt, ast.Try) or type(stmt).__name__ == "TryStar":
            _bind_block(stmt.body, scope, package)
            for handler in stmt.handlers:
                _bind_block(handler.body, scope, package)
            _bind_block(stmt.orelse, scope, package)
            _bind_block(stmt.finalbody, scope, package)
        elif isinstance(stmt, ast.Match):
            for case in stmt.cases:
                _bind_block(case.body, scope, package)
        elif isinstance(stmt, ast.TypeAlias if hasattr(ast, "TypeAlias") else ()):
            _bind_target(stmt.name, scope.names)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _summary_markdown(label: str, refs: list[ImportRef], missing: list[Missing]) -> str:
    distinct = len({r.target for r in refs})
    lines = [f"### Hermes imports against {label}", ""]
    if not missing:
        lines.append(f"All {distinct} Hermes names the plugin imports resolve ({len(refs)} import sites).")
        return "\n".join(lines) + "\n"
    lines.append(f"**{len(missing)} of {distinct} Hermes names the plugin imports are missing.**")
    lines.append("")
    lines.append("| Missing | Why | Imported at |")
    lines.append("|---|---|---|")
    for m in missing:
        sites = "<br>".join(f"`{r.path}:{r.line}`" for r in m.refs)
        lines.append(f"| `{m.target}` | {m.reason} | {sites} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--hermes", required=True, type=Path, help="checkout of NousResearch/hermes-agent")
    parser.add_argument("--src", default=Path(__file__).resolve().parent.parent / "src", type=Path,
                        help="plugin source folder (default: this repo's src/)")
    parser.add_argument("--label", default=None, help="name of the Hermes ref, for the output")
    parser.add_argument("--summary", type=Path, default=None,
                        help="append a markdown summary to this file (GitHub step summary)")
    parser.add_argument("--list", action="store_true", help="print every Hermes import found")
    args = parser.parse_args(argv)

    if not (args.hermes / "gateway").is_dir():
        print(f"error: {args.hermes} is not a hermes-agent checkout (no gateway/ folder)", file=sys.stderr)
        return 2
    if not args.src.is_dir():
        print(f"error: {args.src} is not a folder", file=sys.stderr)
        return 2
    label = args.label or str(args.hermes)

    try:
        refs = collect_imports(args.src)
    except SyntaxError as exc:
        print(f"error: cannot parse {exc.filename}:{exc.lineno}: {exc.msg}", file=sys.stderr)
        return 2
    if not refs:
        print(f"error: found no Hermes imports in {args.src}; the check would pass by finding nothing",
              file=sys.stderr)
        return 2

    tree = HermesTree(args.hermes)
    try:
        missing = tree.check(refs)
    except SyntaxError as exc:
        print(f"error: cannot parse Hermes file {exc.filename}:{exc.lineno}: {exc.msg}", file=sys.stderr)
        return 2

    if args.list:
        for ref in refs:
            print(f"  {ref.target:<60} {ref.path}:{ref.line}")

    distinct = len({r.target for r in refs})
    if missing:
        print(f"Hermes imports against {label}: {len(missing)} of {distinct} names MISSING")
        for m in missing:
            print(f"MISSING {m.target}: {m.reason}")
            for ref in m.refs:
                print(f"    imported at {ref.path}:{ref.line}")
    else:
        print(f"Hermes imports against {label}: all {distinct} names resolve ({len(refs)} import sites)")

    if args.summary is not None:
        with args.summary.open("a", encoding="utf-8") as fh:
            fh.write(_summary_markdown(label, refs, missing))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
