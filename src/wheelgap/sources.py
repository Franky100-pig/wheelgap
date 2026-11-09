"""Read packages (and, where the lockfile records them, their distribution files) from inputs."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

LOCKFILES = ("uv.lock", "pylock.toml", "poetry.lock", "pdm.lock", "Pipfile.lock", "requirements.txt")


class SourceError(ValueError):
    pass


@dataclass
class Package:
    name: str
    version: str | None
    # Distribution filenames. None means "unknown, ask the index".
    files: list[str] | None = None
    # Set for packages that don't come from an index (editable, git, path, ...).
    skip: str | None = None
    error: str | None = None
    origin: str = ""
    # Environment markers; the package is installed if any of them is true. None means always.
    markers: list[str] | None = None

    @property
    def key(self) -> str:
        return canonicalize_name(self.name)


# A dependency edge in a lockfile graph: (name, version or None, marker or None)
Edge = tuple[str, str | None, str | None]


@dataclass
class Source:
    label: str
    packages: list[Package] = field(default_factory=list)
    # uv.lock only: which packages each package depends on, and where the walk starts.
    # When set, a package applies to a target only if it's reachable along edges whose markers hold.
    edges: dict[int, list[Edge]] | None = None
    roots: list[int] = field(default_factory=list)
    root_edges: list[Edge] = field(default_factory=list)
    # Index into root_edges -> name of the dependency group it comes from, e.g. {0: "dev"}.
    root_groups: dict[int, str] = field(default_factory=dict)


def _filename(entry: dict) -> str | None:
    for k in ("filename", "file", "name"):
        if isinstance(entry.get(k), str) and entry[k].endswith((".whl", ".tar.gz", ".zip", ".tar.bz2", ".tgz")):
            return entry[k]
    url = entry.get("url")
    if isinstance(url, str):
        return unquote(Path(urlparse(url).path).name)
    path = entry.get("path")
    if isinstance(path, str):
        return Path(path).name
    return None


def _uv_edges(p: dict) -> list[Edge]:
    groups = [p.get("dependencies", [])]
    for table in ("optional-dependencies", "dev-dependencies"):
        groups.extend(p.get(table, {}).values())
    return [(d["name"], d.get("version"), d.get("marker")) for g in groups for d in g]


def parse_uv_lock(path: Path) -> Source:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    manifest = data.get("manifest", {})
    members = {canonicalize_name(m) for m in manifest.get("members", [])}
    edges: dict[int, list[Edge]] = {}
    source = Source(path.name, edges=edges)
    for p in data.get("package", []):
        src = p.get("source", {})
        pkg = Package(p["name"], p.get("version"), origin=path.name)
        kind = next((k for k in ("editable", "virtual", "git", "path", "directory", "url") if k in src), None)
        if kind and "registry" not in src:
            pkg.skip = kind
        else:
            files = [f for f in (_filename(w) for w in p.get("wheels", [])) if f]
            if p.get("sdist"):
                files.append(_filename(p["sdist"]) or f"{p['name']}-{p.get('version')}.tar.gz")
            pkg.files = files
        i = len(source.packages)
        source.packages.append(pkg)
        edges[i] = _uv_edges(p)
        if kind in ("editable", "virtual", "directory") or pkg.key in members:
            source.roots.append(i)
    for name, group in manifest.get("dependency-groups", {}).items():
        for d in group:
            source.root_groups[len(source.root_edges)] = name
            source.root_edges.append((d["name"], None, d.get("marker")))
    source.root_edges.extend((d["name"], None, d.get("marker")) for d in manifest.get("requirements", []))
    if not source.roots and not source.root_edges:
        source.edges = None  # no idea where the graph starts; treat every package as needed
    return source


def parse_pylock(path: Path) -> Source:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    pkgs = []
    for p in data.get("packages", []):
        pkg = Package(p["name"], p.get("version"), origin=path.name)
        if p.get("marker"):
            pkg.markers = [p["marker"]]
        if any(k in p for k in ("vcs", "directory", "archive")) and "wheels" not in p and "sdist" not in p:
            pkg.skip = next(k for k in ("vcs", "directory", "archive") if k in p)
        else:
            files = [f for f in (_filename(w) for w in p.get("wheels", [])) if f]
            if p.get("sdist"):
                files.append(_filename(p["sdist"]) or f"{p['name']}-{p.get('version')}.tar.gz")
            pkg.files = files
        pkgs.append(pkg)
    return Source(path.name, pkgs)


def parse_poetry_lock(path: Path) -> Source:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    legacy_files = data.get("metadata", {}).get("files", {})
    pkgs = []
    for p in data.get("package", []):
        pkg = Package(p["name"], p.get("version"), origin=path.name)
        markers = p.get("markers")
        if isinstance(markers, str):
            pkg.markers = [markers]
        elif isinstance(markers, dict):
            pkg.markers = [m for m in markers.values() if isinstance(m, str)] or None
        src_type = p.get("source", {}).get("type")
        if src_type in ("git", "hg", "svn", "bzr", "directory", "file", "url"):
            pkg.skip = src_type
        else:
            entries = p.get("files")
            if entries is None:
                entries = legacy_files.get(p["name"], [])
            pkg.files = [f for f in (_filename(e) for e in entries) if f]
        pkgs.append(pkg)
    return Source(path.name, pkgs)


def _add_once(pkgs: list[Package], seen: dict[tuple[str, str | None], Package], pkg: Package) -> None:
    """Append `pkg` unless the same name and version is already there; then widen that entry's markers.

    Pipenv lists a package used by both default and develop in both, and PDM writes one entry per
    extras variant (`coverage` and `coverage[toml]`). Either way it's one install, needed if any entry is.
    """
    key = (pkg.key, pkg.version)
    first = seen.get(key)
    if first is None:
        seen[key] = pkg
        pkgs.append(pkg)
    elif first.markers is not None:
        first.markers = None if pkg.markers is None else first.markers + pkg.markers


def parse_pdm_lock(path: Path) -> Source:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    pkgs: list[Package] = []
    seen: dict[tuple[str, str | None], Package] = {}
    for p in data.get("package", []):
        pkg = Package(p["name"], p.get("version"), origin=path.name)
        if p.get("marker"):
            pkg.markers = [p["marker"]]
        src_type = next((k for k in ("git", "hg", "svn", "bzr", "path", "url", "file") if k in p), None)
        if src_type:
            pkg.skip = src_type
        elif "files" in p:
            pkg.files = [f for f in (_filename(e) for e in p["files"]) if f]
        # No files list at all: leave files as None so the PyPI lookup fills them in.
        _add_once(pkgs, seen, pkg)
    return Source(path.name, pkgs)


def parse_pipfile_lock(path: Path) -> Source:
    data = json.loads(path.read_text(encoding="utf-8"))
    pkgs: list[Package] = []
    seen: dict[tuple[str, str | None], Package] = {}
    for section in ("default", "develop"):
        for name, p in data.get(section, {}).items():
            version = p.get("version")
            pkg = Package(name, version.removeprefix("==") if version else None, origin=path.name)
            if p.get("markers"):
                pkg.markers = [p["markers"]]
            pkg.skip = next((kind for kind in ("git", "hg", "svn", "bzr", "path", "file") if kind in p), None)
            if pkg.skip is None and p.get("editable"):
                pkg.skip = "editable"
            _add_once(pkgs, seen, pkg)
    return Source(path.name, pkgs)


def _pinned_version(req: Requirement) -> str | None:
    specs = list(req.specifier)
    if len(specs) == 1 and specs[0].operator in ("==", "===") and "*" not in specs[0].version:
        return specs[0].version
    return None


def requirement_to_package(text: str, origin: str) -> Package:
    try:
        req = Requirement(text)
    except InvalidRequirement as e:
        raise SourceError(f"{origin}: can't parse requirement {text!r}: {e}") from None
    markers = [str(req.marker)] if req.marker else None
    if req.url:
        return Package(req.name, None, skip="url", origin=origin, markers=markers)
    return Package(req.name, _pinned_version(req), origin=origin, markers=markers)


_NAMED_URL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\s*(\[[^\]]*\])?\s*@\s")


def parse_requirements(path: Path, _seen: set[Path] | None = None) -> Source:
    seen = _seen if _seen is not None else set()
    path = path.resolve()
    if path in seen:
        return Source(path.name)
    seen.add(path)
    pkgs: list[Package] = []
    lines = path.read_text(encoding="utf-8").replace("\\\n", " ").splitlines()
    for raw in lines:
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("-r ", "--requirement")):
            ref = line.split(None, 1)[1].strip() if " " in line else line.split("=", 1)[1]
            pkgs.extend(parse_requirements(path.parent / ref, seen).packages)
            continue
        if line.startswith("-e ") or line.startswith("--editable"):
            name = line.split("#egg=", 1)[1] if "#egg=" in line else line.split(None, 1)[-1]
            pkgs.append(Package(name, None, skip="editable", origin=path.name))
            continue
        if line.startswith("-"):
            continue  # pip options: --index-url, -c, --hash continuation, ...
        line = " ".join(t for t in line.split() if not t.startswith("--hash"))
        if line.startswith((".", "/")):
            pkgs.append(Package(line, None, skip="path", origin=path.name))
            continue
        if "://" in line and not _NAMED_URL_RE.match(line):
            # A bare URL (git+ssh://git@github.com/org/repo.git#egg=foo): never from the index.
            # "name @ url" lines are left to Requirement, which knows the name and markers.
            name = line.split("#egg=", 1)[1].split("&")[0] if "#egg=" in line else line
            pkgs.append(Package(name, None, skip="url", origin=path.name))
            continue
        pkgs.append(requirement_to_package(line, path.name))
    return Source(path.name, pkgs)


PARSERS = {
    "uv.lock": parse_uv_lock,
    "pylock.toml": parse_pylock,
    "poetry.lock": parse_poetry_lock,
    "pdm.lock": parse_pdm_lock,
    "Pipfile.lock": parse_pipfile_lock,
}


def load_file(path: Path) -> Source:
    name = path.name
    if name in PARSERS:
        return PARSERS[name](path)
    if name.startswith("pylock.") and name.endswith(".toml"):
        return parse_pylock(path)
    if name.endswith(".lock") and "uv" in name:
        return parse_uv_lock(path)
    if name.endswith((".txt", ".in")):
        return parse_requirements(path)
    raise SourceError(
        f"don't know how to read {path} "
        "(expected uv.lock, pylock.toml, poetry.lock, pdm.lock, Pipfile.lock or requirements.txt)"
    )


def find_lockfile(directory: Path) -> Path:
    for name in LOCKFILES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    raise SourceError(
        f"no lockfile found in {directory} (looked for {', '.join(LOCKFILES)}). "
        "Pass a file, or package names like `wheelgap numpy psycopg2`."
    )


def load_inputs(inputs: list[str]) -> list[Source]:
    """Each input is a file, a directory (lockfile auto-detected) or a requirement like 'numpy==2.3.4'."""
    sources: list[Source] = []
    adhoc = Source("command line")
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            sources.append(load_file(find_lockfile(p)))
        elif p.is_file():
            sources.append(load_file(p))
        elif "/" in item or item.endswith((".lock", ".txt", ".toml")):
            raise SourceError(f"{item}: no such file")
        else:
            adhoc.packages.append(requirement_to_package(item, "command line"))
    if adhoc.packages:
        sources.append(adhoc)
    return sources
