"""Every name imported from `.const` must actually be defined in const.py.

No Home Assistant, no network. The modules are read by source and parsed, not
imported, because importing the package pulls in Home Assistant. Run from the
repo root:

    python tests/test_const_exports.py

This exists because 0.7.0 shipped an integration that would not import at all:
`__init__.py` did `from .const import CONF_CLIP_RECORDING, DEFAULT_CLIP_RECORDING`
and const.py defined neither, so `async_setup_entry` never ran, the sidebar
panel never registered, and the entry sat in "cannot connect". Nothing in CI
imports `__init__.py` (it needs a running Home Assistant), so a missing const
reached a release. A source-level parse needs no Home Assistant and catches the
whole class: a name imported from const that const does not define.
"""

from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components/blink_liveview_proxy"
CONST = COMPONENT / "const.py"

FAILURES: list[str] = []


def check(condition: bool, label: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  {status}  {label}")
    if not condition:
        FAILURES.append(label)


def _defined_names(tree: ast.AST) -> set[str]:
    """Top-level names const.py makes importable: assignments, defs and imports."""
    names: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def _const_imports(tree: ast.AST) -> list[str]:
    """Names a module pulls in via `from .const import ...`."""
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module == "const":
            names.extend(alias.name for alias in node.names)
    return names


def main() -> int:
    print("\nconst.py defines every name imported from it")

    defined = _defined_names(ast.parse(CONST.read_text()))
    check(bool(defined), "const.py parsed and has module-level names")

    for path in sorted(COMPONENT.glob("*.py")):
        if path.name == "const.py":
            continue
        wanted = _const_imports(ast.parse(path.read_text()))
        missing = [name for name in wanted if name not in defined]
        check(
            not missing,
            f"{path.name}: every `from .const import` name is defined"
            + (f" (missing {missing})" if missing else ""),
        )

    if FAILURES:
        print(f"\n{len(FAILURES)} failed")
        return 1
    print("\nall passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
