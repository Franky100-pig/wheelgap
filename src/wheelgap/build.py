"""--explain's build info: what a `source` build needs, from the sdist's own `[build-system] requires`.

Only what the requirements prove is said. A requirement wheelgap doesn't recognise is shown as it is written, and
"no compiler needed" is only claimed for backends that can't run a compiler, whatever the project configures.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import quote

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from . import cache, pypi
from .sources import Package

RUST = "a Rust toolchain"
CMAKE = "CMake and a C++ compiler"
C_COMPILER = "a C compiler"
NEEDS = {
    "maturin": RUST,
    "setuptools-rust": RUST,
    "scikit-build-core": CMAKE,
    "scikit-build": CMAKE,
    "cmake": CMAKE,
    "cython": C_COMPILER,
    "meson-python": C_COMPILER,
}
# Backends that never compile anything. hatchling, pdm-backend and setuptools are left out: build hooks (configured
# in hatch.toml, or pdm_build.py, which pdm-backend loads by itself) and setup.py can compile with nothing else in
# `requires`, and those files aren't read. So they're shown as written, like any other requirement.
PURE = {"flit-core", "flit"}


@dataclass(frozen=True)
class BuildInfo:
    requires: tuple[str, ...]  # [build-system] requires, as written
    build_script: bool = False  # [tool.poetry] build: a script poetry-core runs, which may compile


def describe(info: BuildInfo) -> str | None:
    """'a Rust toolchain (maturin), setuptools', 'no compiler (flit-core)', or None when requires is empty."""
    needs: dict[str, list[str]] = {}
    unknown: list[str] = []
    pure: list[str] = []
    for text in info.requires:
        try:
            name = Requirement(text).name
        except InvalidRequirement:
            unknown.append(text.strip())
            continue
        key = canonicalize_name(name)
        if key in NEEDS:
            needs.setdefault(NEEDS[key], []).append(name)
        elif key in PURE or (key == "poetry-core" and not info.build_script):
            pure.append(name)
        else:
            unknown.append(name)
    if not needs and not unknown:
        return f"no compiler ({', '.join(dict.fromkeys(pure))})" if pure else None
    # Names once each: requires may list one twice under different markers (Cython for older and newer Pythons).
    known = [
        f"{label} ({', '.join(dict.fromkeys(names))})"
        for label in (RUST, CMAKE, C_COMPILER)
        if (names := needs.get(label))
    ]
    return ", ".join(known + list(dict.fromkeys(unknown)))


def _parse(pyproject: dict | None) -> tuple[list[str] | None, bool]:
    """(requires, build_script) from a parsed pyproject.toml; requires is None when it doesn't list them."""
    if not isinstance(pyproject, dict):
        return None, False
    system = pyproject.get("build-system")
    requires = system.get("requires") if isinstance(system, dict) else None
    if not (isinstance(requires, list) and all(isinstance(r, str) for r in requires)):
        requires = None
    tool = pyproject.get("tool")
    poetry = tool.get("poetry") if isinstance(tool, dict) else None
    return requires, isinstance(poetry, dict) and "build" in poetry


def lookup(pkg: Package, read_cache: bool = False, write_cache: bool = False) -> BuildInfo | None:
    """The build info of a package's release, or None: no .tar.gz sdist, no [build-system] requires, or any failure.

    The sdist's URL comes from the index's JSON API (cached file lists keep no URLs). A .zip sdist isn't read.
    """
    sdists = [f for f in pkg.files or [] if f.endswith(".tar.gz")]
    if not sdists or not pkg.version or not pypi.may_look_up(pkg.registry):
        return None
    base = pypi.index_url()
    entry = cache.load_build(base, pkg.key, pkg.version) if read_cache else None
    if entry is not None:
        requires, script = entry["requires"], entry["build_script"]
    else:
        try:
            data = pypi.fetch_json(f"{base}/{quote(pkg.key)}/{quote(pkg.version)}/json")
            url = next(u["url"] for u in data["urls"] if u.get("filename") == sdists[0] and isinstance(u["url"], str))
            requires, script = _parse(pypi.fetch_sdist_pyproject(url))
        except Exception:  # no answer: the report is the same as without build info
            return None
        if write_cache:
            cache.save_build(base, pkg.key, pkg.version, requires, script)
    return None if requires is None else BuildInfo(tuple(requires), script)


def lookup_all(
    packages: list[Package], read_cache: bool = False, write_cache: bool = False, workers: int = 16
) -> dict[tuple[str, str], BuildInfo]:
    """Build info by (key, version), one lookup per release however many targets or inputs list it, in parallel."""
    todo = list({(p.key, p.version): p for p in packages if p.version}.values())
    with ThreadPoolExecutor(max_workers=workers) as pool:
        found = pool.map(lambda p: lookup(p, read_cache, write_cache), todo)
        return {(p.key, p.version or ""): info for p, info in zip(todo, found, strict=True) if info is not None}
