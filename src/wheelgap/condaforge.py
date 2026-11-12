"""--conda-forge: say when conda-forge has a package of the same name with builds for a gap's platform.

Only a hint under --explain: a conda-forge build doesn't make `pip install` work, and a conda package of the same
name may be another project, so it never changes a status, the summary or the exit code.
"""

from __future__ import annotations

import urllib.error
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from . import cache, pypi
from .sources import Package
from .targets import Target

# One package's files on anaconda.org, each with attrs.subdir and labels.
API = "https://api.anaconda.org/package/conda-forge"
# Uncompressed, numpy's answer is about 7.5 MB and pyarrow's over 60 MB; gzipped, about a sixteenth of that.
# Past the cap, the lookup counts as failed and prints nothing.
MAX_BYTES = 16 * 1024 * 1024
WORKERS = 4
NOARCH = "noarch"  # builds that install on every subdir
MAIN = "main"  # the label `conda install -c conda-forge` reads; "broken" and the cf2019xx labels are left out

# wheelgap platform -> conda subdir (conda's KNOWN_SUBDIRS). Only glibc Linux, macOS and Windows have one:
# musl, Pyodide, iOS and Android targets never match, not even a noarch build.
SUBDIRS = {
    "linux-x86_64": "linux-64",
    "linux-aarch64": "linux-aarch64",
    "linux-armv7l": "linux-armv7l",
    "linux-ppc64le": "linux-ppc64le",
    "linux-s390x": "linux-s390x",
    "linux-riscv64": "linux-riscv64",
    "linux-i686": "linux-32",
    "macos-x86_64": "osx-64",
    "macos-arm64": "osx-arm64",
    "win-amd64": "win-64",
    "win-arm64": "win-arm64",
    "win32": "win-32",
}


def index_allows(simple_index: str | None) -> bool:
    """Whether this run may look anything up: not when packages come from another index (--index-url, or
    WHEELGAP_INDEX_URL set to a mirror), which may hold private names alongside PyPI's."""
    return simple_index is None and pypi.index_url() == pypi.DEFAULT_INDEX


def may_send(pkg: Package) -> bool:
    """Whether the package's name may go to anaconda.org: it comes from PyPI. A private index's names stay private.
    (A run that index_allows() refuses looks nothing up at all.)"""
    return not pkg.skip and pypi.is_pypi(pkg.registry)


def wanted(targets: Iterable[Target]) -> bool:
    """Whether any of these gap targets could match a conda-forge build."""
    return any(t.platform in SUBDIRS for t in targets)


def parse_subdirs(data: object) -> list[str]:
    """The subdirs with a build on the main label, from an anaconda.org package answer."""
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list):
        raise ValueError("unexpected response")
    found = set()
    for f in files:
        attrs = f.get("attrs") if isinstance(f, dict) else None
        labels = f.get("labels") if isinstance(f, dict) else None
        if isinstance(attrs, dict) and isinstance(labels, list) and MAIN in labels:
            subdir = attrs.get("subdir")
            if isinstance(subdir, str):
                found.add(subdir)
    return sorted(found)


def look_up(key: str, read_cache: bool = False, write_cache: bool = False) -> list[str] | None:
    """The subdirs conda-forge's package named `key` has builds for: empty when there's no such package,
    None when the lookup failed (which prints nothing)."""
    if read_cache and (cached := cache.load_subdirs(API, key)) is not None:
        return cached
    try:
        subdirs = parse_subdirs(pypi.fetch_json(f"{API}/{quote(key)}", timeout=60.0, attempts=2, max_bytes=MAX_BYTES))
    except urllib.error.HTTPError as e:
        if e.code != 404:
            return None
        subdirs = []  # no package of that name
    except Exception:  # network trouble, a response over the cap, an unexpected answer: no hint
        return None
    if write_cache:
        cache.save_subdirs(API, key, subdirs)
    return subdirs


def look_up_all(keys: Iterable[str], read_cache: bool = False, write_cache: bool = False) -> dict[str, list[str]]:
    """Each name's subdirs, for the lookups that worked."""
    todo = list(dict.fromkeys(keys))
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        found = list(pool.map(lambda k: look_up(k, read_cache, write_cache), todo))
    return {k: subdirs for k, subdirs in zip(todo, found, strict=True) if subdirs is not None}


def matches(pkg: Package, targets: Iterable[Target], found: dict[str, list[str]]) -> dict[Target, list[str]]:
    """For each gap target with a matching build: its own subdir and/or noarch, whichever conda-forge has."""
    subdirs = found.get(pkg.key) if may_send(pkg) else None
    if not subdirs:
        return {}
    out = {}
    for t in targets:
        own = SUBDIRS.get(t.platform)
        if own is not None and (hit := [s for s in (own, NOARCH) if s in subdirs]):
            out[t] = hit
    return out


def hint(pkg: Package, matched: dict[Target, list[str]]) -> str:
    """`numpy: conda-forge has a package named numpy with builds for linux-aarch64, osx-arm64`."""
    subdirs = list(dict.fromkeys(s for hit in matched.values() for s in hit if s != NOARCH))
    if any(NOARCH in hit for hit in matched.values()):
        subdirs.append(f"{NOARCH} (any platform)")
    return f"{pkg.name}: conda-forge has a package named {pkg.key} with builds for {', '.join(subdirs)}"
