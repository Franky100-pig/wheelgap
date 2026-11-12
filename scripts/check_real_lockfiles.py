"""Check that wheelgap still reads real projects' lockfiles (#227).

    uv run python scripts/check_real_lockfiles.py                # every project's current lockfile
    uv run python scripts/check_real_lockfiles.py pluggy cleo    # just these (the folder names in sources.toml)
    uv run python scripts/check_real_lockfiles.py --pinned       # the copies in tests/fixtures/real/, offline

tests/fixtures/real/sources.toml lists real lockfiles from open-source projects: two uv.lock, two poetry.lock, a
pdm.lock, a pylock.toml and a Pipfile.lock. By default this downloads each project's current file from its default
branch (raw.githubusercontent.com, no token) and reads it with wheelgap. tests/test_real_lockfiles.py runs the same
checks on the pinned copies, offline. A lockfile fails when wheelgap can't read it, or reads it in a way that would
give a wrong or `unknown` answer for a reason that isn't the index's:

- it has no packages;
- a package has a parse error, or a version or environment marker that doesn't parse (or the lock's
  requires-python doesn't);
- in a lockfile that records filenames (all but Pipfile.lock), a package has no files, a file that's neither a
  wheel nor an sdist, or a wheel whose name doesn't parse, so some target would be `unknown`; or the whole lock
  has no wheels, only sdists (how a renamed wheels table would look);
- a package is skipped for a reason other than being a local project (editable, virtual, directory or path), e.g.
  "git" or "private index": the projects here are chosen to install everything else from PyPI;
- a package is `n/a` on every target (CPython 3.8 to the newest, on every `-t all` platform), which means its
  markers or the dependency graph were misread.

Each failure is printed with the project's name, and the exit code is 1. .github/workflows/lockfile-corpus.yml runs
this every week. It needs the network (except with --pinned), so the tests never run its download.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from packaging.markers import InvalidMarker, Marker
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import InvalidWheelFilename, parse_wheel_filename
from packaging.version import InvalidVersion, Version

import wheelgap
from wheelgap.check import SDIST_SUFFIXES, Status, check
from wheelgap.sources import Source, load_file

CORPUS = Path(__file__).parents[1] / "tests" / "fixtures" / "real"
MANIFEST = CORPUS / "sources.toml"
# Every reader except Pipfile.lock's gets each package's filenames from the lock, so needs no index lookup.
RECORDS_FILES = {"uv", "pylock", "poetry", "pdm"}
# Skips that mean "the project itself, or a path in its repo": anything else is a misread for these projects.
LOCAL_SKIPS = {"editable", "virtual", "directory", "path"}
PYTHONS = ["3.8", "3.9", "3.10", "3.11", "3.12", "3.13", "3.14", "3.14t", "next"]
TARGETS = wheelgap.make_targets(PYTHONS, ["all"])


def manifest() -> list[dict]:
    with MANIFEST.open("rb") as f:
        return tomllib.load(f)["lockfile"]


def _marker_problem(marker: str | None, where: str) -> str | None:
    if not marker:
        return None
    try:
        Marker(marker)
    except InvalidMarker:
        return f"{where}: marker {marker!r} doesn't parse"
    return None


def problems(path: Path) -> tuple[Source | None, list[str]]:
    """Read one lockfile as wheelgap does; return what it read and every reason it doesn't look right."""
    try:
        source = load_file(path)
    except Exception as e:  # any failure to read is what this check is for
        return None, [f"can't read it: {type(e).__name__}: {e}"]
    out: list[str] = []
    if not source.packages:
        out.append("no packages")
    if source.requires_python is not None:
        try:
            SpecifierSet(source.requires_python)
        except InvalidSpecifier:
            out.append(f"requires-python {source.requires_python!r} doesn't parse")
    for edges in (source.edges or {}).values():
        for name, _version, marker in edges:
            if problem := _marker_problem(marker, f"dependency on {name}"):
                out.append(problem)
    for name, _version, marker in source.root_edges:
        if problem := _marker_problem(marker, f"root dependency on {name}"):
            out.append(problem)
    for pkg in source.packages:
        label = f"{pkg.name} {pkg.version}"
        if pkg.skip:
            if pkg.skip not in LOCAL_SKIPS:
                out.append(f"{label}: skipped as {pkg.skip}")
            continue
        if pkg.error:
            out.append(f"{label}: {pkg.error}")
        try:
            Version(pkg.version or "")
        except InvalidVersion:
            out.append(f"{pkg.name}: version {pkg.version!r} doesn't parse")
        for marker in pkg.markers or []:
            if problem := _marker_problem(marker, label):
                out.append(problem)
        if source.kind not in RECORDS_FILES:
            continue
        if not pkg.files:
            out.append(f"{label}: no files read ({'none recorded' if pkg.files is None else 'an empty list'})")
            continue
        for f in pkg.files:
            if f.endswith(".whl"):
                try:
                    parse_wheel_filename(f)
                except InvalidWheelFilename:
                    out.append(f"{label}: wheel name {f!r} doesn't parse")
            elif not f.endswith(SDIST_SUFFIXES):
                out.append(f"{label}: {f!r} is neither a wheel nor an sdist")
    files = [f for pkg in source.packages for f in pkg.files or []]
    if source.kind in RECORDS_FILES and files and not any(f.endswith(".whl") for f in files):
        # Every package would be `source`: a renamed wheels table reads like this.
        out.append(f"no wheels read, only sdists ({len(files)})")
    rows = check([source], TARGETS)
    for row in rows:
        label = f"{row.package.name} {row.package.version}"
        if not row.needed:
            out.append(f"{label}: n/a on every target (a misread marker or dependency graph?)")
        elif source.kind in RECORDS_FILES and Status.UNKNOWN in row.results.values():
            out.append(f"{label}: unknown on {sum(s is Status.UNKNOWN for s in row.results.values())} targets")
    return source, list(dict.fromkeys(out))


def download(repo: str, path: str, folder: Path) -> Path:
    """The file at `path` on the default branch (HEAD) of a GitHub repo, saved under its own name in `folder`."""
    url = f"https://raw.githubusercontent.com/{repo}/HEAD/{path}"
    request = urllib.request.Request(url, headers={"User-Agent": "wheelgap-lockfile-corpus"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    dest = folder / Path(path).name
    dest.write_bytes(data)
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("names", nargs="*", help="projects to check, by folder name in sources.toml (default: all)")
    parser.add_argument("--pinned", action="store_true", help="check the copies in tests/fixtures/real/, offline")
    args = parser.parse_args(argv)
    entries = manifest()
    folders = {e["file"].split("/")[0] for e in entries}
    if unknown := sorted(set(args.names) - folders):
        parser.error(f"not in sources.toml: {', '.join(unknown)} (known: {', '.join(sorted(folders))})")
    failed = []
    for e in entries:
        folder = e["file"].split("/")[0]
        if args.names and folder not in args.names:
            continue
        where = "pinned copy" if args.pinned else "default branch"
        name = f"{e['repo']} {e['path']} ({where})"
        with tempfile.TemporaryDirectory() as tmp:
            try:
                path = CORPUS / e["file"] if args.pinned else download(e["repo"], e["path"], Path(tmp))
            except (urllib.error.URLError, OSError) as err:
                source, found = None, [f"can't download it: {err}"]
            else:
                source, found = problems(path)
        if found:
            failed.append(name)
            print(f"FAIL {name}")
            for problem in found:
                print(f"    {problem}")
        else:
            assert source is not None
            print(f"ok   {name}: {len(source.packages)} packages")
    if failed:
        print(f"\n{len(failed)} lockfile{'s' if len(failed) != 1 else ''} failed: {'; '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
