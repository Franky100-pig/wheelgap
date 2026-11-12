"""--explain --build-info: what a `source` build needs, from the [build-system] table of the sdist's pyproject.toml.

Only sdists of releases that positively come from PyPI (or the WHEELGAP_INDEX_URL mirror) are read, the file is
the one pip would read (see pypi.fetch_sdist_pyproject), and where any of that is in doubt there is no answer.
What is said is what the requirements prove: a requirement wheelgap doesn't map is shown by name, and "no compiler"
only follows from a backend that can't run one.
"""

from __future__ import annotations

import http.client
import posixpath
import re
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from . import cache, pypi
from .check import marker_holds
from .sources import Package
from .targets import Target

RUST = "a Rust toolchain"
CMAKE = "CMake and a C/C++ compiler"
COMPILER = "a C/C++ compiler"
NEEDS = {
    "maturin": RUST,
    "setuptools-rust": RUST,
    "scikit-build-core": CMAKE,
    "scikit-build": CMAKE,
    "cmake": CMAKE,
    "cython": COMPILER,
    "meson-python": COMPILER,
}
POETRY = "poetry.core.masonry.api"
# Backends that can't run a compiler: poetry-core's only without a build script, and none of them in-tree.
PURE_BACKENDS = {"flit_core.buildapi", "flit.buildapi", POETRY}
_BACKEND = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*(:[A-Za-z_]\w*(\.[A-Za-z_]\w*)*)?", re.ASCII)
_SPECIFIER = re.compile(r"[A-Za-z0-9.*+!=<>~,_-]*", re.ASCII)  # `===` takes any text: that isn't shown

TIMEOUT = 10.0  # seconds, per request
DEADLINE = 30.0  # seconds for all of them
WORKERS = 4

Release = tuple[str, str, str]  # (index, name, version): one lookup each


@dataclass(frozen=True)
class BuildInfo:
    requires: tuple[Requirement, ...]
    backend: str | None  # None: pip uses setuptools' legacy backend
    backend_path: bool  # an in-tree backend
    build_script: bool  # [tool.poetry] build: a script poetry-core runs, which may compile

    @property
    def pure(self) -> bool:
        if self.backend not in PURE_BACKENDS or self.backend_path:
            return False
        return not (self.backend == POETRY and self.build_script)


def parse(pyproject: dict | None) -> dict | None:
    """What wheelgap keeps (and caches) from a pyproject.toml, or None where pip wouldn't use it as it stands: no
    [build-system] table (pip then uses setuptools), or one pip rejects, such as a requirement that doesn't parse."""
    system = pyproject.get("build-system") if isinstance(pyproject, dict) else None
    if not isinstance(system, dict):
        return None
    requires, backend, path = system.get("requires"), system.get("build-backend"), system.get("backend-path", [])
    if not (isinstance(requires, list) and all(isinstance(r, str) for r in requires)):
        return None
    if backend is not None and not (isinstance(backend, str) and _BACKEND.fullmatch(backend)):
        return None
    if not (isinstance(path, list) and all(isinstance(p, str) for p in path)):
        return None
    try:
        for text in requires:
            Requirement(text)
    except InvalidRequirement:
        return None
    tool = pyproject.get("tool") if isinstance(pyproject, dict) else None
    poetry = tool.get("poetry") if isinstance(tool, dict) else None
    script = isinstance(poetry, dict) and "build" in poetry
    return {"requires": requires, "backend": backend, "backend_path": bool(path), "build_script": script}


def _info(answer: dict | None) -> BuildInfo | None:
    if answer is None:
        return None
    try:
        requires = tuple(Requirement(text) for text in answer["requires"])
    except InvalidRequirement:
        return None
    return BuildInfo(requires, answer["backend"], answer["backend_path"], answer["build_script"])


def shown(req: Requirement) -> str:
    """A requirement as wheelgap prints it: its normalized name, extras and specifiers, never the text as written."""
    name: str = canonicalize_name(req.name)
    if req.extras:
        name += f"[{','.join(sorted(canonicalize_name(e) for e in req.extras))}]"
    if req.url:
        return f"{name} (direct URL)"
    spec = str(req.specifier)
    return name + spec if _SPECIFIER.fullmatch(spec) else name


def requires_for(info: BuildInfo, targets: list[Target]) -> list[Requirement]:
    """The requirements whose marker holds (or may hold) on at least one of `targets`."""
    return [r for r in info.requires if r.marker is None or any(marker_holds(str(r.marker), t) for t in targets)]


def describe(info: BuildInfo, targets: list[Target]) -> str | None:
    """'at least a Rust toolchain (maturin>=1,<2), setuptools', 'no compiler (flit-core>=3.2)', or None."""
    needs: dict[str, list[str]] = {}
    others: list[str] = []
    for req in requires_for(info, targets):
        label = NEEDS.get(canonicalize_name(req.name))
        (needs.setdefault(label, []) if label else others).append(shown(req))
    items = [
        f"{label} ({', '.join(dict.fromkeys(names))})"
        for label in (RUST, CMAKE, COMPILER)
        if (names := needs.get(label))
    ]
    items += dict.fromkeys(others)
    if info.backend_path and info.backend:
        items.append(f"in-tree backend {info.backend}")
    if info.pure and not needs:
        return f"no compiler ({', '.join(items)})" if items else "no compiler"
    return f"at least {', '.join(items)}" if items else None


def _sdist(pkg: Package) -> str | None:
    """The release's one sdist, if it's a .tar.gz: with several, which one pip picks isn't certain."""
    sdists = [f for f in pkg.files or [] if not f.endswith(".whl")]
    return sdists[0] if len(sdists) == 1 and sdists[0].endswith(".tar.gz") else None


def where(pkg: Package) -> tuple[Release, str | None, str | None] | None:
    """(release, sdist URL, sha256) for a release whose sdist positively comes from PyPI or the WHEELGAP_INDEX_URL
    mirror, or None. The URL is the lock's own (uv.lock, pylock.toml), or None to ask the JSON API this run looked
    the package up on. Anything else (a pdm.lock or pylock.toml entry without an index, a private index, a direct
    URL) isn't looked up: its name may be private, and a namesake on PyPI is another package."""
    sdist = _sdist(pkg)
    if sdist is None or not pkg.version:
        return None
    if pkg.sdist_url and isinstance(pkg.registry, str) and pypi.may_look_up(pkg.registry):
        if unquote(posixpath.basename(urlsplit(pkg.sdist_url).path)) != sdist:
            return None
        return (pkg.registry.rstrip("/"), pkg.key, pkg.version), pkg.sdist_url, pkg.sdist_sha256
    if pkg.looked_up_on is not None and pkg.looked_up_on == pypi.index_url():
        return (pkg.looked_up_on, pkg.key, pkg.version), None, None
    return None


class _Phase:
    """Shared by the lookups of one run: a deadline, and whether to stop (a connection failed, or time ran out)."""

    def __init__(self, deadline: float):
        self.deadline = deadline
        self.stopped = False
        self.out_of_time = False

    def timeout(self) -> float:
        return max(0.1, min(TIMEOUT, self.deadline - pypi._clock()))


def _locate(release: Release, sdist: str, phase: _Phase) -> tuple[str | None, str | None]:
    """The sdist's URL and sha256 from the JSON API (the cached file lists keep no URLs)."""
    index, name, version = release
    data = pypi.fetch_json(f"{index}/{quote(name)}/{quote(version)}/json", timeout=phase.timeout(), attempts=1)
    entry = next((u for u in data["urls"] if isinstance(u, dict) and u.get("filename") == sdist), None)
    if entry is None or not isinstance(entry.get("url"), str):
        return None, None
    digests = entry.get("digests")
    sha256 = digests.get("sha256") if isinstance(digests, dict) else None
    return entry["url"], sha256 if isinstance(sha256, str) else None


def lookup(pkg: Package, phase: _Phase, read_cache: bool = False, write_cache: bool = False) -> BuildInfo | None:
    """A release's build info, or None. An answer that can't change (including "no answer") is cached; a network
    failure isn't, and it stops the lookups still to come."""
    found = where(pkg)
    sdist = _sdist(pkg)
    if found is None or sdist is None:
        return None
    release, url, sha256 = found
    if read_cache and (hit := cache.load_build(*release)) is not None:
        return _info(hit["answer"])
    if phase.stopped:
        return None
    if pypi._clock() >= phase.deadline:
        phase.stopped = phase.out_of_time = True
        return None
    try:
        if url is None:
            url, sha256 = _locate(release, sdist, phase)
            if url is None:
                return None
        answer = parse(pypi.fetch_sdist_pyproject(url, sha256, phase.deadline, phase.timeout()))
    except pypi.SdistRefused:
        answer = None
    except pypi.DeadlineExceeded:
        phase.stopped = phase.out_of_time = True
        return None
    except urllib.error.HTTPError:  # the server answered: the others may still work
        return None
    except (OSError, http.client.HTTPException):  # no connection, or a timeout: don't wait on the rest too
        phase.stopped = True
        return None
    except Exception:  # a hash mismatch, a truncated download, an odd JSON answer: no answer, and not cached
        return None
    if write_cache:
        cache.save_build(*release, answer)
    return _info(answer)


def release_of(pkg: Package) -> Release | None:
    found = where(pkg)
    return found[0] if found else None


def lookup_all(
    packages: list[Package], read_cache: bool = False, write_cache: bool = False
) -> tuple[dict[Release, BuildInfo], float | None]:
    """Build info by release (see where), one lookup per release however many targets or inputs list it, at most
    WORKERS at a time and DEADLINE seconds in all. Also returns the seconds taken when time ran out, else None."""
    todo: dict[Release, Package] = {}
    for p in packages:
        if (release := release_of(p)) is not None:
            todo.setdefault(release, p)
    start = pypi._clock()
    phase = _Phase(start + DEADLINE)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        infos = list(pool.map(lambda p: lookup(p, phase, read_cache, write_cache), todo.values()))
    found = {release: info for release, info in zip(todo, infos, strict=True) if info is not None}
    return found, (pypi._clock() - start if phase.out_of_time else None)
