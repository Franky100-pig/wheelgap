<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/wheelgap/wheelgap/main/docs/logo/wheelgap-lockup-dark.png">
    <img alt="wheelgap" src="https://raw.githubusercontent.com/wheelgap/wheelgap/main/docs/logo/wheelgap-lockup.png" height="72">
  </picture>
</h1>

[![CI](https://github.com/wheelgap/wheelgap/actions/workflows/ci.yml/badge.svg)](https://github.com/wheelgap/wheelgap/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/wheelgap)](https://pypi.org/project/wheelgap/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/wheelgap/wheelgap/blob/main/LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
[![good first issues](https://img.shields.io/github/issues/wheelgap/wheelgap/good%20first%20issue?label=good%20first%20issues&color=7057ff)](https://github.com/wheelgap/wheelgap/labels/good%20first%20issue)

**Find the Python dependencies that have no prebuilt wheel for your target platform, before your Docker build does.**

Point it at your lockfile and the platforms you ship to (ARM servers, Alpine images, Windows on ARM, free-threaded Python 3.14t). It tells you which packages pip or uv will have to compile from source, and which can't be installed at all.

📊 **New: [wheel coverage for the top 5,000 PyPI packages](https://wheelgap.github.io/tracker/)**, by platform and Python version, updated nightly.

![wheelgap demo](https://raw.githubusercontent.com/wheelgap/wheelgap/main/docs/demo.gif)

## Install and run (under 30 seconds)

With [uv](https://docs.astral.sh/uv/):

```bash
uvx wheelgap
```

Or with pipx:

```bash
pipx run wheelgap
```

Or install it: `pip install wheelgap`.

Run either one in a project directory. It finds `uv.lock`, `pylock.toml`, `poetry.lock`, `pdm.lock`, `Pipfile.lock`, `requirements.lock`, `requirements.txt` or `pixi.lock` on its own.

## Usage

See the [recipes](https://github.com/wheelgap/wheelgap/blob/main/docs/recipes.md) for multi-arch images, monorepos with several lockfiles, and a free-threading readiness report you can use in scripts.

```bash
# Will my Alpine ARM image compile anything?
wheelgap -p 3.13 -t alpine-arm64

# Is my stack ready for free-threaded Python 3.14?
wheelgap -p 3.14t -t all

# What blocks the move to the newest CPython? (next is 3.15 in this release)
wheelgap -p 3.14 -p next -t all

# Several Pythons at once, Linux on both architectures
wheelgap uv.lock -p 3.12,3.13,3.14 -t linux-x86_64,linux-aarch64

# No -p? A .python-version file next to uv.lock (written by uv or pyenv) sets the Python
wheelgap uv.lock -t linux-x86_64

# Check a Pipenv project's default and development dependencies
wheelgap Pipfile.lock -p 3.13 -t linux-x86_64,linux-aarch64

# Check a PDM project's locked distribution files
wheelgap pdm.lock -p 3.13 -t linux-x86_64,linux-aarch64

# Check the PyPI packages of a pixi project's `test` environment (default: `default`)
wheelgap pixi.lock --environment test -p 3.13 -t linux-x86_64,macos-arm64

# No lockfile? Check packages directly (latest version, or pin one)
wheelgap psycopg2 uvloop numpy==2.3.4 -t windows,win-arm64

# Take the target from the final stage's image (e.g. python:3.13-alpine
# or ghcr.io/astral-sh/uv:0.13.7-python3.13-trixie-slim), following
# named stages like `FROM base AS runtime` back to the image they start from.
# A python X.Y tag without a Debian release (python:3.13-slim) is read as
# the release Docker Hub currently builds it on (trixie, glibc 2.41, for 3.9
# and later); patch tags like 3.12.7-slim keep the --glibc default. Pin
# -bookworm or -trixie for an answer that doesn't move
wheelgap --dockerfile Dockerfile

# Machine-readable, for scripts and CI
wheelgap --json > wheelgap.json

# Why is it a gap? List each rejected wheel and the reason
wheelgap -t alpine-arm64 --explain
```

Projects usually ship to the same platforms, so you can write the defaults down once in `pyproject.toml` (next to the lockfile, or in the current directory). Command-line flags still win.

```toml
[tool.wheelgap]
python = ["3.13", "3.14t"]
targets = ["linux-x86_64", "linux-aarch64", "musl-aarch64"]
glibc = "2.31"
```

For uv projects, wheelgap also reads `[tool.uv] required-environments` as default targets:

```toml
[tool.uv]
required-environments = ["sys_platform == 'linux' and platform_machine == 'aarch64'"]
```

Target selection uses `-t` first, then `--dockerfile`, then `[tool.wheelgap] targets`, then uv's
`required-environments`, then the built-in defaults. Each marker is evaluated against the eight common
native platforms using the selected Python versions. A platform is included if any marker matches
for any selected Python, and the resulting platforms are checked for all selected Pythons.
Linux markers select both glibc and musl targets, since markers cannot distinguish the libc;
the example above selects `linux-aarch64` and `musl-aarch64`. Use `-t` to choose one explicitly.
An inferred selection is explained on stderr, including with `--json` or `--markdown`.
The inferred targets replace the built-in defaults, so a project that only requires Intel macOS
wheels is then checked for Intel macOS only: add `[tool.wheelgap] targets` or `-t` to check more.
An absent or empty setting keeps the built-in defaults, and so does one matching none of the
targets above (with a note). An invalid marker is an error. Only `required-environments` is used here: `environments`, extra
Linux architectures, mobile and Pyodide targets are not inferred.

Each cell in the table is one of:

| cell | meaning |
| --- | --- |
| `✓` | a compatible wheel exists, so the install is fast |
| `● source` | no wheel, only an sdist, so it compiles at install time (needs a compiler, headers, time) |
| `✗ missing` | no wheel and no sdist, so the install fails |
| `–` | not installed on this target (environment markers exclude it) |
| `✓ bundled` | no wheel on PyPI, but Pyodide ships its own build (with `--pyodide-lock`) |

Packages that are fine everywhere are hidden; `--all` shows them.

With several targets, the summary also counts the gaps on each target that has any (`musl-aarch64  1 from source, 1 missing`). `--markdown` lists the same counts. `--json` has them for every target, with zeros where there are no gaps and also when only one target is checked, as `summary.per_target`, keyed by target label: `{"linux-aarch64": {"source": 0, "missing": 0, "unknown": 0}, "musl-aarch64": {"source": 1, "missing": 1, "unknown": 0}}`.

`--explain` lists, for each gap, the wheels that were rejected and why, closest misses first:

```
armlib 1.4.0 on musl-aarch64: ● source
  armlib-1.4.0-cp313-cp313-manylinux_2_17_aarch64.manylinux2014_aarch64.whl  manylinux_2_17_aarch64 is a glibc wheel; musl (Alpine) needs musllinux
```

For `uv.lock` inputs, and `poetry.lock` files older than lock-version 2.1 (which wheelgap reads as a dependency graph), `--explain` also shows why the package is installed at all: the shortest dependency path from your project (or from a dependency group, shown as `(dev group)`) to the gap package. With extra/group selection it's a path the selection installs it through, not always the shortest. Paths are worked out per target, since markers can pull a package in on one platform only. With `--json --explain`, each package gets a matching `via` object keyed by target, like `rejected`: `"via": {"win-arm64": ["myapp", "uvicorn", "uvloop"]}`. Other inputs have no dependency graph, so they show no path (and `"via": {}`).

The shape of `--json` output is described by [`docs/wheelgap-output.schema.json`](https://github.com/wheelgap/wheelgap/blob/main/docs/wheelgap-output.schema.json). It matches the wheelgap version it ships with, and later versions may add keys. In `--json` output, `results`, `rejected` and `via` are all keyed by target label: just the platform (`"linux-x86_64"`) when one Python is checked, or Python and platform (`"3.13 linux-x86_64"`) with several. The top-level `targets` list gives each target's `label`, `python` and `platform`, so a script can look the key up instead of building it:

```python
import json

data = json.load(open("wheelgap.json"))
key = next(t["label"] for t in data["targets"] if t["python"] == "3.13" and t["platform"] == "linux-x86_64")
```

```
uvloop 0.21.0 on win-arm64: ● source
  via: myapp → uvicorn → uvloop
```

With `--build-info` (or `build-info = true` in `[tool.wheelgap]`), `--explain` also says what each `source` build needs, from the `[build-system]` table in the sdist's own `pyproject.toml`, once per package:

```
rpds-py 0.27.0 on musl-armv7l: ● source
  build needs: at least a Rust toolchain (maturin<2.0,>=1.9)
```

- `maturin` and `setuptools-rust` mean a Rust toolchain; `scikit-build-core`, `scikit-build` and `cmake` mean CMake and a C/C++ compiler; `Cython` and `meson-python` mean a C/C++ compiler. Every other requirement is listed by name and version range (`at least a C/C++ compiler (cython>=3.0), setuptools, wheel`), since wheelgap can't tell what it implies. A requirement with an environment marker counts only if the marker holds on one of the targets that build it.
- `no compiler` is only said for a `build-backend` that can't run one, named in `requires` from an index: `flit_core.buildapi` (from `flit-core` or `flit`), `flit.buildapi` (from `flit`), or `poetry.core.masonry.api` (from `poetry-core`, without a `build` script). Without a `build-backend`, pip uses setuptools, which can compile anything; an in-tree backend (`backend-path`) is named as such.
- With `--json --explain --build-info`, each package gets `build_requires`: the same requirements, normalized (`["maturin<2.0,>=1.9"]`), or `null`.
- It's off by default because it downloads: lockfiles are otherwise read without contacting the network. Only packages known to come from PyPI are read, by the same rule `--conda-forge` follows: a `uv.lock` or `pylock.toml` package from PyPI, from the URL in the lock if it's on `files.pythonhosted.org` and names that release's sdist, checked against the lock's sha256; or a package wheelgap itself found on pypi.org from an input that names no other index (no `--index-url` line, no index settings in the environment). Packages from `pdm.lock`, `poetry.lock` or a `pylock.toml` entry without an `index`, private indexes and direct URLs are never looked up, `--online` included, so their names don't reach pypi.org; and nothing is in a run with `--index-url` or `WHEELGAP_INDEX_URL`.
- The whole `.tar.gz` is read in memory, never written to disk, up to 64 MB downloaded, and it gives no answer wherever pip could read another `pyproject.toml` from it than wheelgap would, or would fail: several top folders, links out of the sdist or written through, links alongside non-ASCII names (macOS folds those), names Windows reads as others (a trailing dot or space, `:`, `~`, a backslash), repeated names, a name differing only in case. `.zip` sdists, releases with more than one sdist, and sdists without a `[build-system]` table get no answer either.
- At most 30 seconds go to it in all, with one 10-second attempt per download, and the first connection failure stops the rest. Either way, a note on stderr says how many packages weren't read (not with `-q`). Otherwise a failure prints nothing, and build info never changes the exit code. Answers are cached for 24 hours with the other lookups, but only from a download that matched its sha256. `--offline` turns it off.

If you think wheelgap got a package wrong, that output (or `--json --explain`) is all a bug report needs, with no need to share your lockfile.

`--conda-forge` adds a hint to `--explain`: for a package with a gap on a glibc Linux, macOS or Windows target, it asks [anaconda.org](https://anaconda.org/conda-forge) whether conda-forge has a package with exactly the same (canonical) name and a build of the same version that fits that target. For example, `wheelgap numpy==2.3.4 -p 3.14t -t linux-ppc64le --explain --conda-forge` adds:

```
numpy: conda-forge has a package named numpy with 2.3.4 builds for linux-ppc64le
```

A build fits when all of these hold:

- It's on conda-forge's `main` label and not marked broken.
- It's for the target's conda subdir (`linux-64`, `linux-aarch64`, `linux-ppc64le`, `linux-riscv64`, `osx-64`, `osx-arm64`, `win-64`, `win-arm64`) or `noarch`.
- Its `__glibc` and `__osx` minimums hold for the target's glibc and macOS versions (`--glibc`, `--macos`); conda-forge's riscv64 builds need glibc 2.39, for example. A `noarch` build that depends on `__win`, `__unix`, `__linux`, `__osx` or `__glibc` fits only those OSes, and one that needs another virtual package (such as `__cuda`) never counts.
- It's for the target's Python:
  - a compiled build by its `python_abi` dependency: the same version and ABI, free-threaded or not;
  - an abi3 build (`_python_abi3_support`) for any non-free-threaded CPython its `cpython` constraint admits;
  - a `noarch` build by its `python` constraints.

  A build whose Python can't be told doesn't count.

musl, Pyodide, iOS and Android targets never match, and neither do `linux-i686`, `linux-armv7l`, `linux-s390x` and `win32`, where conda-forge builds no current Python. With `--json --explain`, the package gets a `conda_forge` object: `"conda_forge": {"name": "numpy", "version": "2.3.4", "builds": {"linux-ppc64le": ["linux-ppc64le"]}}`, keyed by target label like `rejected`, with the subdirs of the builds that fit.

It's only a hint, so it never changes a status, the summary or the exit code: a conda-forge build doesn't make `pip install` work, and conda names don't always match PyPI's (PyPI's `torch` is `pytorch` there). Without the flag nothing is looked up. With it, only names known to be on PyPI are sent to anaconda.org. Either a lockfile package's registry or index is exactly `https://pypi.org/simple` (`uv.lock`, `pylock.toml`), or this run found the package on pypi.org and nothing points pip or uv at another index:

- **The input names no other index.** That's package names, a requirements file with no `-i`/`--index-url`, `--extra-index-url`, `-f`/`--find-links` or `--no-index` option (in any form pip reads, in it or in the files it pulls in with `-r` and `-c`), a `pyproject.toml` with no index or sources in `[tool.uv]`, `[tool.uv.pip]`, `[tool.poetry]`, `[tool.pdm]` or `[tool.pixi.pypi-options]`, or a `Pipfile.lock` whose sources are all PyPI. A `-c` file that isn't read (a URL or `${VAR}`) or an abbreviated `--requirement`/`--constraint` option counts as naming one.
- **No `uv.toml`, `pyproject.toml` or `pdm.toml` sets an index** in the input's folder or a folder above it, up to the repository root (the first with a `.git`). One that can't be read counts as setting one.
- **No environment variable sets one:** `PIP_INDEX_URL`, `PIP_EXTRA_INDEX_URL`, `PIP_FIND_LINKS`, `PIP_NO_INDEX`, `PIP_CONFIG_FILE` (any case after `PIP_`), `UV_INDEX_URL`, `UV_DEFAULT_INDEX`, `UV_INDEX`, `UV_EXTRA_INDEX_URL`, `UV_FIND_LINKS`, `UV_NO_INDEX`, `UV_CONFIG_FILE` or any `UV_INDEX_*`.

User and system settings aren't read: a `pip.conf` or `pip.ini`, `~/.config/uv/uv.toml`, `/etc/uv/uv.toml` and `.netrc`. If those point pip or uv at a private index, don't use `--conda-forge` (or set one of the variables above). Others, such as `pdm.lock`, `poetry.lock` and `pixi.lock` packages, aren't looked up, and a note on stderr names them. A run with `--index-url`, `index-url` or `WHEELGAP_INDEX_URL` looks nothing up, and `--offline` refuses the flag. Each request is tried once (with a 15-second socket timeout), all of them together get 30 seconds, and the first that can't reach anaconda.org stops the rest. Answers over 16 MB (pyarrow's is about 60 MB) are refused, so at most about 250 MB is in use. When a lookup fails or is dropped, a note on stderr says no hint isn't a "no". Answers, including "too large" and "no such package", are cached for 24 hours like pinned lookups (`--no-cache` skips the cache); failures aren't.

**Exit codes:** `0` no gaps, `1` gaps found, `2` bad input. `--exit-zero` exits `0` even when gaps are found; bad input still exits `2`. `--fail-on missing` (or `fail-on = "missing"` in `[tool.wheelgap]`) accepts source builds and only exits `1` when a package can't be installed at all. Gaps on `--advisory` targets ([below](#in-ci-github-action)) never change the exit code.

If an input can't be read (a missing lockfile, an unknown target, a bad `[tool.wheelgap]` value), `--json` prints `{"error": {"message": "...", "type": "TargetError"}}` on stdout instead of results, next to the usual `wheelgap: error:` line on stderr and exit code 2. Command-line usage errors from argparse are still plain text on stderr.

Use `--ignore psycopg2` for accepted gaps (repeatable or comma-separated, with case-insensitive package-name matching and equivalent hyphens, underscores and dots): matched packages are excluded from the table, counts and exit status, and listed as ignored in text (including quiet mode), JSON and Markdown. Set `ignore = ["psycopg2"]` in `[tool.wheelgap]` to use it by default; any `--ignore` flags replace the configured list rather than adding to it, and `--ignore ""` clears it for one run.

To accept a gap on one target only, add the target after a colon: `--ignore psycopg2:musl-x86_64` (or `ignore = ["psycopg2:musl-x86_64"]`) still checks psycopg2 everywhere else. The target takes anything `-t` does (`alpine`, `linux/amd64`, `all`), one per entry (`--ignore psycopg2:alpine,psycopg2:win-arm64`). An unknown target is an error (exit 2), and so is a second target after a comma (`psycopg2:alpine,win-arm64`), which would otherwise read as a package name. The package keeps its real status in the report (`● source (ignored)` in the table), but a gap on that target doesn't count towards the summary or the exit status, isn't explained by `--explain`, and is a skipped testcase in `--junit`. Reports list it as `ignored: psycopg2 (musl-x86_64)`, and `--json` puts it in `ignored_targets` (`ignored` keeps only packages ignored everywhere). A target that isn't checked in this run gets a note on stderr.

Use `-q` / `--quiet` (or `quiet = true` in `[tool.wheelgap]`) to show only the gap summary and package names. Clean runs without ignored packages are silent, JSON and Markdown output are unchanged, and quiet text output cannot be combined with `--explain` (`--json --explain` and `--markdown --explain` work).

### Check the dependencies you actually install

With no selection options, wheelgap keeps its conservative behavior: all project/workspace extras
and dependency groups count. For `uv.lock` inputs, select a deployment's dependencies explicitly:

```bash
# Match a production uv sync --no-dev --extra api
wheelgap uv.lock --no-dev --extra api -t linux-aarch64
# Include every project extra, or add a dependency group
wheelgap uv.lock --all-extras
wheelgap uv.lock --group docs
# Check only a group, without project dependencies or default groups
wheelgap uv.lock --only-group docs
```

`--extra`, `--group` and `--only-group` are repeatable. Once any selector is used, the project's runtime
dependencies and uv's default groups (`dev` unless `[tool.uv].default-groups` changes it) count,
unless `--only-group` is given;
project extras are included only when requested. `--no-dev` removes `dev`, while `--only-group`
excludes project dependencies and default groups. Combine `--only-group` with `--group` to add more
groups, but not with `--extra`/`--all-extras`. An explicit `dev` group conflicts with `--no-dev`.
Unknown extra/group names are errors. Command-line selectors need a `uv.lock` input, or for groups a Poetry 2.x
`poetry.lock` or a `pdm.lock`, or for `--extra`/`--all-extras` a `pyproject.toml` (see below); configured `groups`
apply to `uv.lock`, Poetry 2.x `poetry.lock` and `pdm.lock` inputs, and configured `extras` only to `uv.lock` inputs.

`poetry.lock` (Poetry 2.x) inputs accept `--group`, `--only-group` and `--no-dev` with Poetry's meanings, and
configured `groups` apply to them too. `main` and every group not marked `optional = true` in
`pyproject.toml` count; `--group` adds a group (like `poetry install --with`), `--no-dev` leaves out `dev`,
and `--only-group` checks only the named groups (`main` only if named). A package counts if any of its groups is
selected, with the markers of those groups. `--extra` isn't supported for `poetry.lock` yet, and a command-line
selection on a Poetry 1.x lock (no per-package groups) is an error; configured `groups` leave a 1.x lock unfiltered.

`pdm.lock` inputs take the same selectors with PDM's meanings, and configured `groups` apply to them too. Every
group the lock was made for (its `[metadata] groups`: `default`, the dev groups and any optional-dependencies groups
locked with `pdm lock -G`) counts, as with a plain `pdm install`; `--no-dev` leaves out `dev` (like
`pdm install --without dev`), and `--only-group` checks only the named groups (`default` only if named), so PDM's
`--prod` is `--only-group default` here. `--group` only checks that the group is locked, since it already counts. A
group that's in `pyproject.toml` but not in the lock is an error (lock it with `pdm lock -G <name>`), as in PDM. A
package counts if any of its groups is selected. `--extra` isn't supported for `pdm.lock`, and a command-line
selection on a lock without package groups (one locked without the `inherit_metadata` strategy) is an error;
configured `groups` leave such a lock unfiltered.

Default selections can also be configured:

```toml
[tool.wheelgap]
extras = ["api"]
groups = ["docs"]
```

CLI `--extra` and `--group` replace their corresponding configured lists. `--only-group` suppresses
both configured lists, and `--no-dev` removes a configured `dev` group. Empty lists explicitly select
no extras or additional groups. With selection enabled, rooted workspaces start at the root project
(members outside the selection aren't checked, and a note on stderr names them);
virtual workspaces start at their members. Dependency-requested extras are followed transitively,
including local workspace dependencies, rather than enabling every extra of every dependency.

Unresolved marker conditions (including uv's synthetic conflict markers and unknown OS releases)
remain conservative: wheelgap can still report a possible gap rather than silently hide one.
Selection itself needs no network. Index refreshes (`--online`, or a target outside the lockfile's
`requires-python`) only look up packages in the selected graph. Configured `extras` only apply to `uv.lock` inputs, and configured `groups` to `uv.lock`, Poetry 2.x
`poetry.lock` and `pdm.lock` inputs; other inputs are checked as usual.

### Check a library's direct dependencies

Libraries usually don't commit a lockfile. Pass the library's `pyproject.toml` to check the
dependencies it declares in `[project] dependencies`:

```bash
wheelgap pyproject.toml -t win-arm64 -t pyodide
# Add [project.optional-dependencies] groups
wheelgap pyproject.toml --extra test
wheelgap pyproject.toml --all-extras
```

Each requirement is read like a line of a requirements file, so ranges and bare names use the newest
matching release and markers are respected. With several `-p` versions, that release isn't filtered by
`Requires-Python` ([#84](https://github.com/wheelgap/wheelgap/issues/84)), so ranges and bare names can
show false gaps. Only direct dependencies are checked: their own dependencies aren't, and wheelgap always
prints a note saying so. Use a lockfile for the full picture. If `[project] requires-python` leaves out a `-p`
Python (every clause counts, as pip applies them), that target is still checked and a note names it; an invalid
`requires-python` is ignored with a note.
`[dependency-groups]` isn't read yet, so `--group`, `--only-group` and `--no-dev` are errors here, and a
`dependencies` listed in `[project] dynamic` is an input error (exit 2), as are dynamic
`optional-dependencies` with `--extra`/`--all-extras` and Poetry dependencies kept in
`[tool.poetry.dependencies]` (check `poetry.lock` instead). `wheelgap .` in a directory with no lockfile
doesn't switch to `pyproject.toml` on its own; the error suggests it. `check_lockfile()` and
`wheelgap diff` don't read `pyproject.toml`.

### Targets

Run `wheelgap --list-targets` to see target aliases and compatibility limits, or add `--json` for a machine-readable list.

If you know the deployment distro rather than its libc version, use `--distro`:

```bash
wheelgap uv.lock -p 3.13 -t linux-aarch64 --distro ubuntu-22.04
wheelgap uv.lock -p 3.13 -t musl-aarch64 --distro alpine
```

It supplies a glibc or musl version only: it does **not** select Python versions or platforms. Keep using `-t` for those platforms, including `musl-*` for Alpine. You can also set `distro = "ubuntu-22.04"` in `[tool.wheelgap]`.

| distro names | libc version | package sources |
| --- | --- | --- |
| `ubuntu-20.04`, `ubuntu-22.04`, `ubuntu-24.04` | glibc 2.31, 2.35, 2.39 | [Focal](https://launchpad.net/ubuntu/focal/+source/glibc), [Jammy](https://packages.ubuntu.com/jammy/libc6), [Noble](https://packages.ubuntu.com/noble/libc6) |
| `debian-11` / `bullseye`, `debian-12` / `bookworm`, `debian-13` / `trixie` | glibc 2.31, 2.36, 2.41 | [Bullseye](https://packages.debian.org/bullseye/libc6), [Bookworm](https://packages.debian.org/bookworm/libc6), [Trixie](https://packages.debian.org/trixie/libc6) |
| `rhel-8` / `rocky-8` / `alma-8`, `rhel-9` / `rocky-9` / `alma-9` | glibc 2.28, 2.34 | [RHEL 8](https://access.redhat.com/errata/RHSA-2021:1585), [RHEL 9](https://access.redhat.com/errata/RHBA-2022:3917) |
| `amazonlinux-2`, `amazonlinux-2023` | glibc 2.26, 2.34 | [Amazon Linux toolchain packages](https://docs.aws.amazon.com/linux/al2023/ug/glibc-gcc-and-binutils.html) |
| `alpine` | musl 1.2 (Alpine 3.13 and later) | [Alpine musl](https://pkgs.alpinelinux.org/package/v3.22/main/x86_64/musl) |

For each libc, an explicit `--glibc` / `--musl` wins, then `--distro`, then a version from `--dockerfile`, then a `glibc` / `musl` config value, then the config `distro`, then the built-in default. A distro only supplies its own libc; the other libc keeps its existing default. Names are case-insensitive; unknown names are errors. For older Alpine, pass `--musl 1.1` explicitly. The text `--list-targets` output also lists distro names; its JSON output remains a list of platforms.

Only x86 Linux takes `manylinux` wheels older than glibc 2.17: every other architecture's first manylinux was `manylinux2014` (glibc 2.17), so pip accepts nothing older there, and neither does wheelgap.

| name | aliases | wheel tags it accepts |
| --- | --- | --- |
| `linux-x86_64` | `linux`, `linux-amd64`, `linux/amd64` | `manylinux_2_*_x86_64` up to `--glibc` (default 2.36), legacy `manylinux2014`/`2010`/`1` |
| `linux-aarch64` | `linux-arm64`, `linux/arm64`, `linux/arm64/v8` | `manylinux_2_*_aarch64` from 2.17 up to `--glibc`, legacy `manylinux2014` |
| `musl-x86_64` | `alpine`, `musl` | `musllinux_1_*_x86_64` up to `--musl` (default 1.2) |
| `musl-aarch64` | `alpine-arm64` | `musllinux_1_*_aarch64` |
| `macos-arm64` | `macos`, `mac` | `macosx_*_arm64` and `universal2` up to `--macos` (default 14.0) |
| `macos-x86_64` | `macos-intel` | `macosx_*_x86_64` and `universal2` |
| `win-amd64` | `windows`, `win` | `win_amd64` |
| `win-arm64` | `windows-arm64` | `win_arm64` |
| `linux-armv7l` | `linux-arm`, `linux-armv7`, `linux/arm/v7` | `manylinux_2_*_armv7l` from 2.17 (32-bit ARM, e.g. Raspberry Pi OS), legacy `manylinux2014`, and plain `linux_armv7l` (which PyPI allows for the Pi) |
| `linux-ppc64le`, `linux-s390x`, `linux-riscv64` | `linux/ppc64le`, `linux/s390x`, `linux/riscv64` | `manylinux_2_*` from 2.17 for that arch (`riscv64` has no legacy `manylinux2014` tag) |
| `linux-i686` | `linux-x86`, `linux-386`, `linux/386` | `manylinux_2_*_i686` up to `--glibc`, legacy `manylinux2014`/`2010`/`1` (32-bit x86) |
| `win32` | `windows-x86`, `win-x86` | `win32` (32-bit Python on Windows) |
| `musl-armv7l`, `musl-ppc64le`, `musl-s390x`, `musl-riscv64`, `musl-i686` | `alpine-armv7`, `alpine-x86` | `musllinux_1_*` for that arch (pip would also take a plain `linux_armv7l` wheel on Alpine, but it's a glibc build, so wheelgap doesn't count it) |
| `pyodide` | `wasm`, `wasm32`, `emscripten` | Pyodide's ABI for that Python: `pyodide_2024_0` (3.12), `pyodide_2025_0` / `pyemscripten_2025_0` (3.13), `pyemscripten_2026_0` (3.14) |
| `ios-arm64` | `ios`, `iphoneos` | `ios_*_arm64_iphoneos` up to `--ios` (default 13.0) |
| `ios-simulator-arm64`, `ios-simulator-x86_64` | `ios-simulator` | `ios_*_arm64_iphonesimulator` / `ios_*_x86_64_iphonesimulator` |
| `android-arm64`, `android-x86_64` | `android` | `android_*_arm64_v8a` / `android_*_x86_64` up to `--android-api` (default 24) |

**iOS and Android** (PEP 730 and PEP 738, Python 3.13+) are checked with `-t mobile` or by name, for apps built with tools like BeeWare's Briefcase. Mobile dependencies are installed by a cross-platform pip run that can only use wheels, so a package with only an sdist shows as `✗ missing`. `--ios` and `--android-api` (or `ios = "15.0"` and `android-api = 28` in `[tool.wheelgap]`) set your app's minimum iOS version and Android API level. wheelgap checks PyPI only: Briefcase and Chaquopy also install from their own package repositories (BeeWare's on anaconda.org, and chaquo.com), which have mobile builds of packages like numpy, so a `✗ missing` there may still install.

**Pyodide (Python in the browser)** only installs wheels: there's no compiler, so a package with only an sdist shows as `✗ missing`, not `● source`. Pyodide wheels are tied to one ABI per Python version and don't work across them. Pyodide also ships its own builds of many popular packages (numpy, pydantic-core and others) that aren't on PyPI. Point `--pyodide-lock` at the release's `pyodide-lock.json` (a path or `https://` URL, also `pyodide-lock = "..."` in `[tool.wheelgap]`) and those count as `✓ bundled`, with a note when Pyodide's version differs from the one you pinned (shown with `-q` too, and `"bundled_version_differs": true` in `--json`). That still passes: `pyodide.loadPackage` gives you Pyodide's version, though `micropip.install` of your exact pin would fail.

```bash
wheelgap -p 3.12 -t pyodide --pyodide-lock https://cdn.jsdelivr.net/pyodide/v0.27.7/full/pyodide-lock.json
```

Docker platform names also work with `-t`, for example `-t linux/amd64,linux/arm64` (also `linux/arm64/v8`, `linux/arm/v7`, `linux/ppc64le`, `linux/s390x`, `linux/riscv64` and `linux/386`, also spelled `linux/i386`).

`-t all` checks the eight common native platforms, and `-t everything` adds the armv7l, ppc64le, s390x, riscv64 and i686 Linux targets (glibc and musl) and `win32`. Pyodide, iOS and Android are only checked when you name them. Python versions take a trailing `t` for free-threaded builds (`3.14t`), which correctly rejects `abi3` wheels. Without `-p`, wheelgap reads a `.python-version` file next to the lockfile (or `pyproject.toml`) if there is one, and otherwise uses the Python it is running on.

### Free-threading and new Python readiness

A new CPython comes out each October, and for months many packages have no wheels for it yet. To see what would build from source if you upgraded, check the Python you use now next to the new one. `-p next` is the newest CPython this wheelgap release knows (3.15), and `-p next-t` is its free-threaded build, so you don't have to look the version up; a note on stderr says which version it is (`-q` drops it). Upgrade wheelgap to move `next` on. `next` works anywhere a Python version does: `-p`, `python = ["3.14", "next"]` in `[tool.wheelgap]`, `wheelgap diff -p`, `make_targets(python="next")` and the MCP tools' `python` argument, but not in `.python-version`, which uv and pyenv read too.

```bash
wheelgap uv.lock -p 3.14 -p next -t linux-x86_64,linux-aarch64
wheelgap uv.lock -p 3.14 -p next-t -t all   # the upgrade and free-threading together
```

When exactly two Python versions are checked and one is newer, the summary names the packages that block the upgrade: those with a gap (`● source` or `✗ missing`) on the newer Python on a platform where the older one has a wheel (or doesn't install the package). Gaps both Pythons share, and targets ignored with `--ignore NAME:TARGET`, don't count. Packages that couldn't be checked aren't listed, only counted (`blocking 3.15: none known (1 package couldn't be checked)`), so an empty list isn't a pass on its own: the summary and the exit code still decide.

```
3 of 41 packages have gaps: 3 will build from source.
blocking 3.15: numpy, pydantic-core
```

`--markdown` and `-q` print the same line, and `--json` has the list as `blocking_newer_python` (`[]` when no package is known to block the upgrade; the key is left out unless two Pythons, one newer, are checked). The MCP server's `check_lockfile` returns it too. Two builds of the same version, such as `-p 3.14 -p 3.14t`, have no newer one, so they get no such line; the [free-threading recipe](https://github.com/wheelgap/wheelgap/blob/main/docs/recipes.md#free-threading-readiness) lists a free-threaded build's gaps for scripts.

### In CI (GitHub Action)

```yaml
- uses: actions/checkout@v7
- uses: wheelgap/wheelgap@v0.9.0
  with:
    args: uv.lock -p 3.13 -t linux-x86_64,linux-aarch64
```

The job fails when a dependency would build from source on a target you ship, for example when a dependency bump drops ARM wheels. The gap table also goes to the run's summary page (set `summary: false` to turn that off).

Each package with a gap is also marked on its own line of the lockfile in the pull request's diff, as a GitHub annotation: an error when it fails the check, or a warning for a source build that `--fail-on missing` allows or a gap only on `--advisory` targets. So a bump that drops a platform is flagged next to the line that bumped it. The Action passes `--github-annotations` for this; in your own workflow steps, pass it yourself. It respects `--ignore`, and `--exit-zero` still annotates. The annotations are printed before the usual output, or to stderr with `--json` and `--markdown` so their stdout stays the same. GitHub shows at most 10 errors and 10 warnings per step, so the tenth one says how many more packages have gaps. Paths are relative to `GITHUB_WORKSPACE` (or the current directory outside Actions). When wheelgap can't be sure which line a package is on (a name and version written twice, or a `pyproject.toml` input), the annotation names the file without a line.

To watch a platform before you support it, such as Windows on ARM, make it advisory: `-t linux-x86_64,win-arm64 --advisory win-arm64` (or `advisory = ["win-arm64"]` in `[tool.wheelgap]`). Its gaps are reported as usual but never fail the run: the column is headed `win-arm64 (advisory)` in the table and the Markdown report, the summary includes a line like `3 gaps in 2 packages on advisory targets linux-aarch64, win-arm64 don't fail the run.` (it counts each package once per advisory target, while the line above it counts packages), and `--json` lists the advisory target labels under `advisory`, while `packages`, `summary.with_gaps` and `summary.per_target` still count those gaps. Advisory works per platform, so it marks every Python checked on that platform (`"3.13 win-arm64"` and `"3.14t win-arm64"`): a free-threaded `3.14t` can't be made advisory on its own. `--advisory` takes anything `-t` does (aliases like `windows-arm64`, Docker names like `linux/arm64`), repeated or comma-separated, and replaces the configured list (`--advisory ""` clears it for one run). It only marks targets, so a target must also be checked: an `--advisory` target that isn't is an error (exit 2), while a configured one that a run doesn't check (`-t linux`, a `--dockerfile` image) gets a note on stderr. A group (`all`, `everything`, `mobile`) means whichever of its targets the run checks, so `--advisory all` makes every checked target advisory, and it's an error only when the run checks none of them. When every checked target is advisory, the run can't fail, not even when a lookup fails during an index outage, so a note on stderr says so (`-q` hides it). With `--fail-on missing`, a source build on a required target still passes and a missing package on one still fails, whatever the advisory targets show. An `--ignore NAME:TARGET` gap on an advisory target stays ignored and isn't counted as an advisory gap. In `--junit` reports an advisory gap is a skipped testcase with the message `advisory: source` (or `missing`, `unknown`), not a failure, so dashboards that mark a build unstable on failed tests don't. In the Action, put `--advisory` in `args`: the summary page shows the same marking. `wheelgap diff` has no `--advisory` yet.

Outside the Action, `--markdown` prints the same report as Markdown, for a job summary (`wheelgap --markdown >> "$GITHUB_STEP_SUMMARY"`) or a PR comment. With `--explain`, the rejected wheels go in a collapsed section. `--junit PATH` also writes a JUnit XML file (one testsuite per target) that GitLab, Jenkins, Azure Pipelines and CircleCI can show as a test report, alongside whatever output mode you use.

### With pre-commit

```yaml
repos:
  - repo: https://github.com/wheelgap/wheelgap
    rev: v0.9.0
    hooks:
      - id: wheelgap
```

The hook runs when `uv.lock`, `poetry.lock`, `pdm.lock`, `pylock.toml`, `Pipfile.lock`, `pixi.lock`, or a `requirements*.txt` or `requirements*.lock` file changes. Put your targets in `[tool.wheelgap]` in `pyproject.toml` so the hook checks the right platforms. Pass extra options with `args:`, for example `args: [--fail-on, missing]`.

### From Python

```python
import wheelgap

targets = wheelgap.make_targets(python="3.13", platforms=["linux-aarch64", "musl-aarch64"])

result = wheelgap.check_package("numpy", "2.3.4", targets)  # looks the files up on PyPI
result.ok  # True if every target has a wheel
result.gaps  # the targets that don't
result.to_dict()  # {"name": ..., "results": {"3.13 musl-aarch64": "wheel", ...}}
result.to_dict(explain=True)  # adds "rejected" and "via", as --json --explain does

for r in wheelgap.check_lockfile("uv.lock", targets):  # markers and the uv graph respected
    print(r.name, r.version, [t.platform for t in r.gaps])
    for target, why in r.reasons.items():  # only targets with a gap
        print(target.label(True), " → ".join(r.via.get(target, ())))  # e.g. app → pandas → numpy
        print(why)
```

For each `source` or `missing` target (not `n/a`, `bundled` or `unknown`, where an `unknown` result's `error` says why), a result explains the gap the way `--explain` does. `result.rejected` maps the target to `(filename, reason)` pairs for every wheel it can't use, closest misses first, such as `abi3 wheels can't be used on free-threaded Python (only abi3t ones can)`, or `yanked` for a wheel that fits but that pip skips because the release has other files it can use. `result.reasons` is the same as text: one `filename  reason` per line, with no column alignment and no limit on the number of wheels (the command line shows 8), or `no wheels published for this version`, plus a line such as `there is an sdist, but pyodide can't build packages from source` on the targets that can't build sdists (Pyodide, iOS and Android). Both are worked out when first read. `result.via` maps the same targets to the dependency path from a root down to the package, as `--explain`'s `via:` line shows it: `("app", "pandas", "numpy")`, or `("(dev group)", "pytest")` when a dependency group pulls it in. `check_lockfile()` fills it for `uv.lock`, and for `poetry.lock` files older than lock-version 2.1, the inputs wheelgap reads as a dependency graph; it's empty for other inputs and for `check_package()`. `to_dict()` leaves all three out, and `to_dict(explain=True)` adds `"rejected"` and `"via"` with the same keys and shapes as `wheelgap --json --explain`: each keyed by target label, `rejected` as `{"file", "reason"}` objects.

The Python API doesn't read `[tool.wheelgap]`: pass targets, groups and the rest as arguments, and drop ignored packages yourself.

`check_lockfile(..., offline=True)` never contacts an index: it raises `wheelgap.sources.SourceError` if an input has no locked file list and skips the re-check for Python versions outside the lock's `requires-python` (so those versions may show false gaps). It cannot be combined with `online=True`.

The package ships a `py.typed` marker (PEP 561), so mypy, pyright and other type checkers use its annotations.

`check_packages([...])` checks many packages in parallel, and `check_package(..., files=[...])` works offline from filenames you already have.

To count packages that Pyodide ships itself, as `--pyodide-lock` does, pass `pyodide_locks=[wheelgap.pyodide.load_pyodide_lock("pyodide-lock.json")]` (a path or https URL, one lock per Python) to `check_packages()`, `check_package()` or `check_lockfile()`. They show as `bundled` on pyodide targets for the lock's Python, and the result's `bundled_version` is the version Pyodide ships. A different version from your pin or range still counts as `bundled` (`ok` stays `True`, as in the CLI), so check `bundled_version_differs`; `to_dict()` includes both as `"bundled"` and `"bundled_version_differs"`. A lock that no pyodide target can use raises `PyodideLockError`, as on the command line.

### Scanning the top PyPI packages

```bash
python -m wheelgap.scan --top 5000 --out coverage.json
```

This checks the latest release of the most-downloaded packages (from [top-pypi-packages](https://hugovk.dev/top-pypi-packages/)) against the eight common platforms (and Pyodide), for Python 3.13, 3.14, 3.14t, 3.15 and 3.15t by default, and writes one JSON file with per-platform totals. It's the data behind the [coverage tracker on the roadmap](https://github.com/wheelgap/wheelgap/discussions/43).

Add `--pyodide-lock PATH_OR_URL` once per Pyodide release (each is for one Python) to count the packages Pyodide ships itself as `bundled` on that Python's `pyodide` target; the per-target `summary` counts them too, and `pyodide_locks` in the JSON lists the locks used. Pyodide targets with no lock get a note on stderr.

### Comparing two lockfiles

```bash
wheelgap diff old/uv.lock uv.lock -p 3.13 -t linux-aarch64,musl-aarch64
wheelgap diff origin/main:uv.lock uv.lock -p 3.13 -t linux-aarch64   # the base branch's lockfile, no checkout needed
```

This checks both lockfiles and lists the packages that got worse on a target (a wheel that became a source build, say) and the new packages with gaps, so a dependency bump that drops a platform fails CI. It exits 1 if there are any, 0 if not, and 2 on bad input. A package that can no longer be checked counts as worse: `unknown`, or now installed from git, a path, a URL or a private index (a new one of those counts as a new gap). Packages are matched by name, so a requirements-file path with no name (`./pkg`, or `-e ./pkg` without `#egg=`) shows up as a new package rather than as the one it replaced. Either input can be a git `REF:PATH` (`origin/main:uv.lock`, `HEAD~1:poetry.lock`) when no file or folder of that name exists: wheelgap reads it with git into a temporary folder under the same file name (with the `pyproject.toml` beside it at that ref, if any), so its format is detected and read as usual. PATH is relative to the repository root: use `REF:./uv.lock` from a subfolder. In a shallow CI checkout, fetch the base ref first. A ref or path git can't find exits 2 with git's message, and so does a missing `git`. The same `-p` and `-t` apply to both lockfiles, and `[tool.wheelgap]` and `.python-version` aren't read yet, so pass them explicitly. `--json` prints one object with `worse` (each package's `old_versions` and `new_versions` lists and, per target, `old_status` and `new_status`), `new` (each new package's `versions` and per-target `status`), `unchecked` and `summary` counts; a skipped cell has the status `skipped` and a `skip_reason` (`git`, `url`, `private index`, ...). `diff` is a subcommand only as the first argument when there's no file or folder named `diff` in the current directory (then it's read as an input, with a note; `python -m wheelgap.diff` always compares, and a pinned `diff==2023.12.6` checks the PyPI package of that name). `python -m wheelgap.diff` works the same way.

### With an AI coding agent

wheelgap ships a **`check-wheels` skill** that teaches coding agents to run it, read its JSON and suggest fixes. It follows the open [Agent Skills](https://agentskills.io/) format.

**Claude Code** (install from this repository as a plugin):

```text
/plugin marketplace add wheelgap/wheelgap
/plugin install wheelgap@wheelgap
```

On Claude Code 2.1.275 or later, `/plugin install wheelgap --marketplace wheelgap/wheelgap` does both in one step.

**Codex, Cursor, GitHub Copilot and others:** copy [`integrations/claude-code/skills/check-wheels/`](integrations/claude-code/skills/check-wheels/) into your project's `.agents/skills/` folder. For a Cursor project rule instead, copy [`integrations/cursor/wheelgap.mdc`](integrations/cursor/wheelgap.mdc) to `.cursor/rules/`.

**As an MCP server:** `wheelgap-mcp` lets any agent that speaks the [Model Context Protocol](https://modelcontextprotocol.io/) call wheelgap as a tool, with no shell access needed. It runs over stdio and has five read-only tools: `check_package` (one package, latest or a given version; `explain: true` adds why each target with a gap can't use its wheels), `check_lockfile`, `check_dockerfile`, `diff_lockfiles` and `list_targets`. `check_lockfile` gives the same answer as `wheelgap PATH --json`, `[tool.wheelgap]` settings included: it returns the packages with gaps, the ones it skipped (git, path and private-index packages, which aren't checked) and any notes. `check_dockerfile` gives the answer of `wheelgap PATH --dockerfile DOCKERFILE --json` (`PATH` defaults to the Dockerfile's folder), plus the `image` it read (`python`, `platform`, `glibc`, `musl`), so an agent can ask before switching `python:3.13-slim` to `-alpine` or adding `--platform=linux/arm64`; a Dockerfile wheelgap can't read is a tool error. `diff_lockfiles` takes an `old` and a `new` lockfile (plus `python` and `targets`) and answers as `wheelgap diff OLD NEW --json` does, with `ok` added: the packages that got worse on a target and the new ones with gaps, so an agent that bumps dependencies can save the old lockfile under its own name (`mkdir -p /tmp/old && git show HEAD:uv.lock > /tmp/old/uv.lock`) and ask whether the upgrade lost a wheel. On `pyodide` targets, `check_package` and `check_lockfile` take an optional `pyodide_lock` (a path or https URL of a `pyodide-lock.json`, as `--pyodide-lock` does), so packages Pyodide ships itself show as `bundled`. Tool calls send progress notifications when the client asks for them and don't stop the server answering `ping`. A cancelled call sends no more progress and no result, but its index lookups still finish before the next call starts. It speaks every MCP revision from 2024-11-05 to 2026-07-28: the `initialize` handshake of 2024-11-05 to 2025-11-25, and 2026-07-28's `server/discover` and per-request `_meta`. Like wheelgap, it needs nothing beyond `packaging`. For Claude Code:

```bash
claude mcp add wheelgap -- uvx --from wheelgap wheelgap-mcp
```

In releases after 0.9.0, `uvx wheelgap mcp` starts the same server. Other clients take the same command (`uvx`) and arguments in their MCP settings; the [MCP setup guide](https://github.com/wheelgap/wheelgap/blob/main/docs/mcp.md) has a copy-paste snippet for Claude Code, Cursor, VS Code, Codex and Claude Desktop. Pass `check_lockfile`, `check_dockerfile` and `diff_lockfiles` an absolute path or a `file://` URI, since the server's working directory may not be your project. If the client shares its [roots](https://modelcontextprotocol.io/specification/2025-06-18/client/roots), a relative path such as `uv.lock` is resolved against the first root that contains it.

<!-- mcp-name: io.github.wheelgap/wheelgap -->

Contributing to wheelgap with an agent? See [AGENTS.md](https://github.com/wheelgap/wheelgap/blob/main/AGENTS.md).

## How it works

- **Version ranges** such as `numpy>=1.26,<2` use the newest matching release with a non-yanked file whose `Requires-Python` permits the selected Python. This applies when every target uses the same Python major/minor version (including a mix of regular and free-threaded builds). Missing or invalid `Requires-Python` metadata is treated as unrestricted. Runs checking several Python versions still choose the newest release matching the range without filtering by Python; per-Python resolution remains planned in [#84](https://github.com/wheelgap/wheelgap/issues/84). Exact pins and bare package names keep their existing behavior.
- **Lockfiles are read offline.** `uv.lock`, `pylock.toml`, `poetry.lock` and `pdm.lock` already record every file published for each pinned version (except a `uv.lock` or `pylock.toml` whose `requires-python` leaves out the Python you check; see below). wheelgap parses the wheel filenames' tags with [`packaging`](https://packaging.pypa.io/) and checks them against the exact tag set each target accepts. That is the same logic pip and uv use to pick a wheel.
- **`requirements.txt`, `requirements.lock` and bare package names** are looked up on the PyPI JSON API. Feed it a fully pinned file (from Rye, `uv pip compile`, `pip-compile` or `uv export`): wheelgap checks the lines you give it and doesn't resolve dependencies. Rye's default (non-universal) locks are resolved for the machine that created them and carry no platform markers. Constraint files (`-c FILE`, `-cFILE`, `--constraint FILE` or `--constraint=FILE`) restrict unpinned requirements without adding packages. Exact constraints select a version; ranges are intersected. A constraint wheelgap can't apply leaves the package `unknown` rather than guessed: one with an environment marker (it can't yet be applied per target), a URL or path, or two different exact versions. Constraint files given as a URL or an environment variable (`-c https://…`, `-c ${PIP_CONSTRAINT}`) are skipped.
- **`Pipfile.lock`** reads `default`, `develop` and any custom package categories (such as `docs` or `tests`) and looks up distribution filenames on PyPI for their pinned versions. Environment markers are preserved; Git, path, file and editable dependencies are skipped as non-index packages. Packages pinned to an `index` that isn't PyPI are skipped too (`private index`), so their names aren't sent to pypi.org. A PyPI mirror counts as a private index here, since wheelgap can't tell it apart from an internal one, unless `WHEELGAP_INDEX_URL` points at the JSON API of that same index (for a source `https://mirror.example.com/simple`, set `WHEELGAP_INDEX_URL=https://mirror.example.com/pypi`): then those packages are looked up there, on the index the lock already names. It must be the same repository, not just the same host: `<repo>/simple` matches `<repo>/pypi` with the same scheme, host and port, so another repository on the same Artifactory or Nexus server stays skipped. The index has to serve PyPI's JSON API, including pinned versions (`<WHEELGAP_INDEX_URL>/<name>/<version>/json`); on a mirror that only serves the simple API, such as the Tsinghua or Aliyun mirrors, those packages come back as lookup errors like `not on index` instead. `--index-url` lookups don't use this.
- **`pixi.lock`** (lock versions 6 and 7; pixi writes 7 since 0.68) checks the PyPI packages of one environment: `default`, or the one `--environment NAME` names. In a project directory, `pixi.lock` is read when no other lockfile is there, or when `--environment` is given; otherwise a note on stderr says which file was checked. Conda packages are skipped (`conda`), since wheelgap checks PyPI wheels only; past five, the text and Markdown output count them instead of listing them (`--json` lists each one). pixi records just the one file it picked for each of the lock's platforms, for the environment's Python and pixi's own glibc and macOS floors, so that can't answer other targets: each PyPI package is also looked up on PyPI for its pinned version, a failed lookup is `unknown`, and `--offline` refuses the file. A package from another index is skipped (`private index`) unless `WHEELGAP_INDEX_URL` points at that same index, as are Git, path and direct-URL packages. A direct URL that points at a file on `files.pythonhosted.org` can't be told apart from a package that came from PyPI, so it's checked as one. A package the lock leaves out on one of its platforms (say `pywin32`, locked for `win-64` only) shows `n/a` on targets with that platform's `sys_platform`, `platform_machine` and Python, and still counts everywhere else; platforms that share those (a `linux-64` and a `linux-64-cuda` with the same subdir) hide a package only when it's missing from all of them. `check_lockfile()` takes `environment=` too; the MCP tools and `[tool.wheelgap]` read the `default` environment only. A lock version wheelgap doesn't know, or YAML outside the plain subset pixi writes, is an error rather than a guess.
- **Environment markers are respected.** A `pywin32 ; sys_platform == 'win32'` dependency won't show up as a Linux gap. For `uv.lock` the dependency graph is walked per target, so transitive platform-specific packages are handled too. On `win32`, `linux-i686`, `musl-i686`, `linux-armv7l` and `musl-armv7l` a `platform_machine` comparison counts as unknown, so the package is still checked. `platform_machine` reports the machine, not the Python: 32-bit Python on 64-bit Windows says `AMD64`, a `linux/386` container on an x86_64 host says `x86_64`, and 32-bit Raspberry Pi OS on a Pi 4 or 5 (which boots a 64-bit kernel) or a `linux/arm/v7` container on an arm64 host can say `aarch64`.
- `--online` re-checks lockfile packages against PyPI. Packages the lock says came from another index keep their locked files, so their names are never sent to PyPI. Use it when a project has uploaded new wheels for an existing version since you locked, or if your `uv.lock` was generated with `environments` restrictions. A `uv.lock` or `pylock.toml` whose `requires-python` leaves out a target's Python (say `==3.13.*`, checked with `-p 3.14t`) may be missing wheels for it, because uv only keeps the wheels that can serve `requires-python`. Without `--online`, wheelgap then also looks up its PyPI packages on the index, keeps the lock's own files if a lookup fails, and prints a note (with `-q`, only when some packages couldn't be re-checked).
- `--offline` never contacts an index, for CI and coding agents without network. Inputs that need a lookup (requirements files, `pyproject.toml`, `Pipfile.lock`, package names, lockfile entries with no file list) make it exit 2 with an error naming the packages, so use a lockfile instead. A `--pyodide-lock` URL is refused too: pass a downloaded copy. The `requires-python` re-check above is skipped, with a note that those Pythons may show false gaps. It can't be combined with `--online`. Set `offline = true` in `[tool.wheelgap]` to make it the project default; `--online` on the command line overrides it for one run.
- **Cache:** lookups of a pinned version (`name==1.2.3`) are saved for 24 hours, so a repeat run needn't ask PyPI again. They're kept in `wheelgap/pinned-v1` inside `~/.cache` on Linux (or `$XDG_CACHE_HOME`), `~/Library/Caches` on macOS and `%LOCALAPPDATA%` on Windows. Each entry holds only the release's file names and yanked flags (or, for `--build-info`, its sdist's `[build-system]` table), named by a hash of the index URL (without any credentials in it), the package and the version; on Linux and macOS, wheelgap creates the folder and its entries readable by you only. `--online` always asks the index again and saves the fresh answer, so later runs use it; `--no-cache` neither reads nor saves the cache for one run. Entries older than 24 hours are deleted, at most once a day, and `wheelgap --clear-cache` deletes all of them (add inputs to check them right after). Both touch only wheelgap's own entry files, and never follow a link. `--conda-forge` answers are kept the same way, cut down to the subdir, Python, ABI and virtual-package needs of each build of the checked version. Unpinned names, version ranges and `--index-url` lookups aren't cached, and neither are lookups from the Python API.
- **Private indexes:** `--index-url URL` (or `index-url` in `[tool.wheelgap]`) sends the PyPI lookups described above to a simple index that serves the JSON form (PEP 691) instead. The URL must be https (plain http only for localhost). Credentials can go in the URL passed to `--index-url` (`https://user:token@host/simple`) and never appear in output; in CI, `--index-url "$PRIVATE_INDEX"` keeps them out of your repo. They're refused in `[tool.wheelgap]`, since `pyproject.toml` is committed. An index that only serves HTML (PEP 503) gets a clear error.

## Why this exists

Missing wheels show up late, in the most expensive place:

- Your `python:3.13-alpine` image on an ARM runner takes 25 minutes because two transitive dependencies don't publish `musllinux_aarch64` wheels, and QEMU is compiling them.
- A build fails with `error: Microsoft Visual C++ 14.0 or greater is required` on a teammate's Snapdragon laptop.
- You try free-threaded Python 3.14t and half your stack quietly falls back to source builds, because `abi3` wheels don't apply there.

Sites like [pyreadiness.org](https://pyreadiness.org) and [free-threaded wheels](https://hugovk.github.io/free-threaded-wheels/) track the top few hundred packages. wheelgap checks your packages, at your pinned versions, for your targets, in about a second, and it can run in CI.

## Roadmap

wheelgap is growing into **caniuse for Python wheels**: an open map of which packages have wheels for which platforms. The CLI stays focused, and around it come a nightly tracker of the most-used PyPI packages, a community knowledge base of workarounds, and campaigns to help upstream projects ship missing wheels. **[Read the roadmap and join the discussion →](https://github.com/wheelgap/wheelgap/discussions/43)**

Next up for the CLI, each with a first step written up and no PR yet:

- "nearest version with a wheel" hints ([#20](https://github.com/wheelgap/wheelgap/issues/20))
- version ranges: picking the newest release that supports each target's Python ([#84](https://github.com/wheelgap/wheelgap/issues/84))

The [roadmap](https://github.com/wheelgap/wheelgap/labels/roadmap) and [help wanted](https://github.com/wheelgap/wheelgap/labels/help%20wanted) labels have more.

## Contributing

Contributions are welcome, and pull requests usually get a review within hours.

- **New here?** Issues labelled [good first issue](https://github.com/wheelgap/wheelgap/labels/good%20first%20issue) say which files to change and what to test.
- **Done one or two?** [help wanted](https://github.com/wheelgap/wheelgap/labels/help%20wanted) issues are bigger, and a good next step.

[CONTRIBUTING.md](https://github.com/wheelgap/wheelgap/blob/main/CONTRIBUTING.md) has the 3-command dev setup, or you can start in the browser with no setup:

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/wheelgap/wheelgap)

Questions are welcome in [Discussions](https://github.com/wheelgap/wheelgap/discussions).

## Thanks

wheelgap is created and maintained by [@patrickballard](https://github.com/patrickballard), with a lot of help from these people. Thank you, all of you!

[![wheelgap's contributors](https://contrib.rocks/image?repo=wheelgap/wheelgap)](https://github.com/wheelgap/wheelgap/graphs/contributors)

## License

[MIT](https://github.com/wheelgap/wheelgap/blob/main/LICENSE)
