"""Compare wheelgap's answers with pip's on real PyPI packages (#226).

    uv run --with pip python scripts/check_against_pip.py                # every package on every target
    uv run --with pip python scripts/check_against_pip.py numpy orjson   # just these packages
    uv run --with pip python scripts/check_against_pip.py -t win-arm64 -t "3.14t linux-x86_64"

For each package, wheelgap looks up the latest release and gives its status on each target. Then pip is asked to
download that release for the target, with `pip download NAME==VERSION --no-deps --only-binary=:all:` and exactly
the tags AGENTS.md lists ("No issue fits? Find a real bug"). The two must agree: pip downloads a wheel exactly when
wheelgap says `wheel`. Every disagreement is printed with the pip command that shows it, and the exit code is 1.
A check that couldn't be made (a failed lookup, or pip failing for a reason other than finding no wheel) is listed
too, and makes the exit code 2 when nothing disagreed.

The pip arguments are written out here from AGENTS.md's rules, not taken from wheelgap's targets.py, on purpose:
pip is the referee, so a regression in targets.py's tag lists must show up as a disagreement rather than be handed
on to pip as well.

It needs the network, so the tests never run it; .github/workflows/accuracy.yml runs it every week. pip's cache is
off, and each package's downloads go to one temporary folder that is emptied after every download and deleted after
the package, so nothing is kept.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from wheelgap import Result, check_packages, make_targets
from wheelgap.check import Status
from wheelgap.targets import DEFAULT_GLIBC, PLATFORMS, Target

# Packages with interesting wheel sets: per-Python builds with and without free-threaded wheels, abi3, Rust and
# mypyc builds with py3-none-<platform> wheels, packages without musllinux or Windows on ARM wheels, a Windows-only
# package without an sdist (pywin32), one that ships wheels only for Windows (mysqlclient), a pure-Python wheel
# (requests) and an sdist-only package (docopt).
PACKAGES = [
    *("numpy", "scipy", "pandas", "pyarrow", "cryptography", "pydantic-core", "psycopg2-binary", "psycopg-binary"),
    *("orjson", "lxml", "grpcio", "pillow", "matplotlib", "scikit-learn", "cffi", "charset-normalizer", "markupsafe"),
    *("pyyaml", "msgpack", "ujson", "regex", "rpds-py", "aiohttp", "multidict", "zstandard", "lz4", "bcrypt"),
    *("pynacl", "greenlet", "gevent", "uvloop", "httptools", "watchfiles", "tokenizers", "safetensors", "jiter"),
    *("ruff", "shapely", "pyproj", "contourpy", "duckdb", "tiktoken", "psutil", "wrapt", "mysqlclient", "pywin32"),
    *("protobuf", "pycryptodome", "pyzmq", "numba", "requests", "docopt"),
]

# The targets #226 lists: manylinux and musllinux on x86_64 and aarch64, Windows on x86_64 and ARM, macOS on Apple
# silicon, a free-threaded Python, and an old glibc (2.17, manylinux2014's, where the legacy aliases come in).
OLD_GLIBC = "2.17"


def default_targets() -> list[Target]:
    native = ["linux-x86_64", "linux-aarch64", "musl-x86_64", "musl-aarch64", "win-amd64", "win-arm64", "macos-arm64"]
    return [
        *make_targets("3.14", native),
        *make_targets("3.14t", "linux-x86_64"),
        *make_targets("3.14", "linux-x86_64", glibc=OLD_GLIBC),
    ]


def label(target: Target) -> str:
    """The target's label, plus its glibc when it isn't wheelgap's default (two targets can differ only in that)."""
    glibc = ".".join(map(str, target.glibc))
    old = PLATFORMS[target.platform][0] == "manylinux" and glibc != DEFAULT_GLIBC
    return target.label(True) + (f" glibc {glibc}" if old else "")


_X86 = ("x86_64", "i686")
_MANYLINUX2014_ARCHES = ("aarch64", "armv7l", "ppc64le", "s390x")


def pip_platforms(target: Target) -> list[str]:
    """The --platform values AGENTS.md says to pass pip for this target. pip takes manylinux and musllinux tags
    literally, so each libc version the target accepts is listed, from its own down."""
    family, arch = PLATFORMS[target.platform]
    if family == "manylinux":
        major, minor = target.glibc
        if major != 2 or arch not in (*_X86, *_MANYLINUX2014_ARCHES):
            raise ValueError(f"AGENTS.md doesn't say which tags to pass pip for {label(target)}")
        floor = 5 if arch in _X86 else 17  # other arches start at manylinux2014 (glibc 2.17)
        out = [f"manylinux_2_{m}_{arch}" for m in range(minor, floor - 1, -1)]
        legacy = (
            [("manylinux2014", 17), ("manylinux2010", 12), ("manylinux1", 5)]
            if arch in _X86
            else [("manylinux2014", 17)]
        )
        out += [f"{name}_{arch}" for name, glibc_minor in legacy if minor >= glibc_minor]
        if arch == "armv7l":
            out.append("linux_armv7l")
        return out
    if family == "musllinux":
        major, minor = target.musl
        return [f"musllinux_{major}_{m}_{arch}" for m in range(minor, 0, -1)]  # 1.2: musllinux_1_2 and musllinux_1_1
    if family == "windows":
        return [arch]
    if family == "macos":
        return [f"macosx_{target.macos[0]}_{target.macos[1]}_{arch}"]  # pip widens it to older macOS itself
    raise ValueError(f"pip can't check {label(target)} with `pip download --platform`")


def pip_args(name: str, version: str, target: Target, dest: str) -> list[str]:
    """The arguments after `pip` that ask whether pip would install a wheel of NAME==VERSION on the target."""
    abi = f"cp{target.major}{target.minor}{'t' if target.freethreaded else ''}"
    platforms = [arg for plat in pip_platforms(target) for arg in ("--platform", plat)]
    return [
        *("download", "--no-deps", "--only-binary=:all:", "--index-url", "https://pypi.org/simple", "-d", dest),
        *platforms,
        *("--python-version", f"{target.major}.{target.minor}", "--implementation", "cp", "--abi", abi),
        f"{name}=={version}",
    ]


@dataclass(frozen=True)
class PipAnswer:
    wheel: str | None = None  # the wheel pip downloaded
    error: str | None = None  # pip failed for a reason other than finding no wheel

    @property
    def text(self) -> str:
        return self.wheel or ("error" if self.error else "no wheel")


def run_pip(args: list[str]) -> PipAnswer:
    """Run `pip download` and read its answer from the downloaded file or the error."""
    dest = Path(args[args.index("-d") + 1])
    # No cache (it would fill the disk) and no user pip.conf (it could point pip at another index).
    env = os.environ | {"PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1"}
    cmd = [sys.executable, "-m", "pip", *args, "--no-cache-dir", "--progress-bar", "off"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=env)
    except subprocess.TimeoutExpired:
        return PipAnswer(error="pip timed out")
    wheels = sorted(p.name for p in dest.glob("*.whl"))
    if proc.returncode == 0 and len(wheels) == 1:
        return PipAnswer(wheel=wheels[0])
    if proc.returncode != 0 and "No matching distribution found" in proc.stderr:
        return PipAnswer()
    last = (proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}").splitlines()[-1]
    return PipAnswer(error=f"pip: {last}")


@dataclass(frozen=True)
class Check:
    name: str
    version: str | None
    target: Target
    wheelgap: Status
    pip: PipAnswer | None  # None: pip wasn't asked, because wheelgap couldn't look the package up
    error: str | None = None

    @property
    def compared(self) -> bool:
        return self.pip is not None and self.pip.error is None and self.wheelgap is not Status.UNKNOWN

    @property
    def agrees(self) -> bool:
        assert self.pip is not None
        return (self.wheelgap is Status.WHEEL) == (self.pip.wheel is not None)

    def command(self) -> str:
        assert self.version is not None
        return shlex.join(["pip", *pip_args(self.name, self.version, self.target, "/tmp/wg-check")])


def check_one(result: Result, targets: Sequence[Target], pip: Callable[[list[str]], PipAnswer]) -> list[Check]:
    """Ask pip about one package on every target, in one temporary folder that's emptied after each download."""
    if result.error or result.version is None:
        error = result.error or "no version"
        return [Check(result.name, result.version, t, Status.UNKNOWN, None, error) for t in targets]
    checks = []
    tmp = tempfile.mkdtemp(prefix="wheelgap-pip-")
    try:
        for t in targets:
            answer = pip(pip_args(result.name, result.version, t, tmp))
            checks.append(Check(result.name, result.version, t, result.statuses[t], answer))
            for leftover in Path(tmp).iterdir():
                leftover.unlink()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return checks


def compare(
    packages: Sequence[str],
    targets: Sequence[Target],
    pip: Callable[[list[str]], PipAnswer] = run_pip,
    workers: int = 4,
) -> list[Check]:
    for t in targets:
        pip_platforms(t)  # fail before any download for a target AGENTS.md has no pip tags for
    results = check_packages(packages, targets)
    checks: list[Check] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for done, found in enumerate(pool.map(lambda r: check_one(r, targets, pip), results), 1):
            bad = sum(c.compared and not c.agrees for c in found)
            print(f"[{done}/{len(results)}] {found[0].name} {found[0].version}: {bad} disagreement(s)", file=sys.stderr)
            checks.extend(found)
    return checks


def _table(rows: list[list[str]]) -> list[str]:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return ["  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip() for row in rows]


def report(checks: list[Check], out=sys.stdout) -> int:
    """Print the disagreements and the checks that couldn't be made. 1 if any disagree, else 2 if any couldn't be
    made, else 0."""
    compared = [c for c in checks if c.compared]
    wrong = [c for c in compared if not c.agrees]
    failed = [c for c in checks if not c.compared]
    print(
        f"{len(checks)} checks: {len(compared) - len(wrong)} agree with pip, {len(wrong)} disagree, "
        f"{len(failed)} couldn't be compared.",
        file=out,
    )
    if wrong:
        print("\nDisagreements (pip has the last word):\n", file=out)
        rows = [["package", "version", "target", "wheelgap", "pip"]]
        rows += [
            [c.name, c.version or "-", label(c.target), c.wheelgap.value, c.pip.text if c.pip else ""] for c in wrong
        ]
        print("\n".join(_table(rows)), file=out)
        print("\npip commands:\n", file=out)
        for c in wrong:
            print(f"# {c.name} {c.version} on {label(c.target)}\n{c.command()}", file=out)
    if failed:
        print("\nCouldn't compare:\n", file=out)
        rows = [["package", "version", "target", "why"]]
        for c in failed:
            why = c.error or (c.pip.error if c.pip and c.pip.error else f"wheelgap says {c.wheelgap.value}")
            rows.append([c.name, c.version or "-", label(c.target), why or ""])
        print("\n".join(_table(rows)), file=out)
    return 1 if wrong else 2 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("packages", nargs="*", help="packages to check (default: the built-in list of about 50)")
    parser.add_argument(
        "-t", "--target", action="append", default=[], help="only this target: a label, or a platform name"
    )
    parser.add_argument("--workers", type=int, default=4, help="packages checked at once (default 4)")
    args = parser.parse_args(argv)
    targets = default_targets()
    if args.target:
        targets = [t for t in targets if label(t) in args.target or t.platform in args.target]
        if not targets:
            parser.error(
                f"no target matches {args.target}; the targets are: {', '.join(map(label, default_targets()))}"
            )
    version = subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True, text=True)
    if version.returncode != 0:
        parser.error("pip isn't installed in this environment; run it with `uv run --with pip python ...`")
    print(version.stdout.strip(), file=sys.stderr)
    packages = args.packages or PACKAGES
    print(
        f"Checking {len(packages)} packages on {len(targets)} targets: {', '.join(map(label, targets))}",
        file=sys.stderr,
    )
    return report(compare(packages, targets, workers=args.workers))


if __name__ == "__main__":
    sys.exit(main())
