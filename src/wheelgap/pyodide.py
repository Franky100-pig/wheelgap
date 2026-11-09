"""Packages that Pyodide ships in its own distribution, read from a release's pyodide-lock.json."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from packaging.utils import canonicalize_name

from .check import Row, Status
from .targets import PLATFORMS, Target

# Pyodide's ABI version for each Python (the `abi_version` in pyodide-lock.json).
PYODIDE_ABI = {(3, 12): "2024_0", (3, 13): "2025_0", (3, 14): "2026_0"}


class PyodideLockError(ValueError):
    pass


@dataclass(frozen=True)
class PyodideLock:
    source: str
    pyodide_version: str
    python: tuple[int, int]
    abi: str | None
    packages: dict[str, str]  # canonical name -> bundled version

    def applies_to(self, target: Target) -> bool:
        return PLATFORMS[target.platform][0] == "pyodide" and (target.major, target.minor) == self.python


def parse_pyodide_lock(data: dict, source: str) -> PyodideLock:
    info = data.get("info")
    packages = data.get("packages")
    if not isinstance(info, dict) or not isinstance(packages, dict):
        raise PyodideLockError(f"{source}: not a pyodide-lock.json (expected 'info' and 'packages')")
    try:
        major, minor = (int(p) for p in str(info["python"]).split(".")[:2])
    except (KeyError, ValueError):
        raise PyodideLockError(f"{source}: info.python is missing or not a version") from None
    abi = info.get("abi_version")
    expected = PYODIDE_ABI.get((major, minor))
    if abi and expected and abi != expected:
        raise PyodideLockError(f"{source}: abi_version {abi} doesn't match Python {major}.{minor} ({expected})")
    bundled = {
        str(canonicalize_name(p["name"])): str(p["version"])
        for p in packages.values()
        # Also listed: CPython stdlib modules, shared libraries and test bundles, which aren't PyPI packages.
        if isinstance(p, dict) and p.get("package_type", "package") == "package" and "name" in p and "version" in p
    }
    return PyodideLock(source, str(info.get("version", "?")), (major, minor), abi, bundled)


def load_pyodide_lock(spec: str) -> PyodideLock:
    """Read a pyodide-lock.json from a path, or fetch it when given an http(s) URL."""
    if spec.startswith(("http://", "https://")):
        from .pypi import fetch_json

        data = fetch_json(spec)
    else:
        try:
            data = json.loads(Path(spec).read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise PyodideLockError(f"{spec}: not valid JSON ({e})") from None
    return parse_pyodide_lock(data, spec)


def check_targets(lock: PyodideLock, targets: list[Target]) -> None:
    if not any(lock.applies_to(t) for t in targets):
        have = ", ".join(sorted({t.python for t in targets if PLATFORMS[t.platform][0] == "pyodide"})) or "none"
        py = f"{lock.python[0]}.{lock.python[1]}"
        raise PyodideLockError(
            f"{lock.source} is for Python {py} (Pyodide {lock.pyodide_version}), "
            f"but no pyodide target uses Python {py} (pyodide targets: {have}); try -p {py} -t pyodide"
        )


def apply_bundled(rows: list[Row], targets: list[Target], lock: PyodideLock) -> None:
    """Mark gaps on matching pyodide targets as BUNDLED when Pyodide ships the package itself.

    Only SOURCE and MISSING become BUNDLED. UNKNOWN means the lookup failed (a typo'd pin, a network
    error), and that must stay a gap rather than pass because the name happens to be in the lock.
    """
    for r in rows:
        if r.package.key not in lock.packages:
            continue
        for t in targets:
            if lock.applies_to(t) and r.results[t] in (Status.SOURCE, Status.MISSING):
                r.results[t] = Status.BUNDLED
