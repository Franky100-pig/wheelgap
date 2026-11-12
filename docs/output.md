# Output

wheelgap prints a table by default, or JSON (`--json`) or Markdown (`--markdown`) instead, and can also write a JUnit
XML file (`--junit PATH`). Notes and errors go to stderr, so the stdout of `--json` and `--markdown` holds only the
report.

## Exit codes

| code | meaning |
| --- | --- |
| `0` | no gaps (or `--exit-zero`) |
| `1` | gaps found, including packages that couldn't be looked up (`unknown`) |
| `2` | bad input: a missing lockfile, an unknown target, a bad `[tool.wheelgap]` value, a usage error |

- `--exit-zero` exits `0` even when gaps are found; bad input still exits `2`.
- `--fail-on missing` (or `fail-on = "missing"`) accepts source builds and only exits `1` when a package can't be
  installed at all, or couldn't be checked.
- `--ignore NAME` and `--ignore NAME:TARGET` take accepted gaps out of the exit status.
- After 0.9.0, gaps on `--advisory` targets never change the exit code.

Add `set -o pipefail` when you pipe the output, so input errors still fail.

## Text

```text
wheelgap 0.9.0 · uv.lock · 5 packages · Python 3.13

package    version  linux-aarch64  musl-aarch64
fastlib    2.0.0    ● source       ● source
nativeext  0.3      ● source       ● source
puresdist  1.0      ● source       ● source

2 packages with wheels for every target hidden (--all to show)
skipped 2 non-index packages: demo (editable), gitdep (git)
linux-aarch64  3 from source
musl-aarch64   3 from source
3 of 5 packages have gaps: 3 will build from source.
Run with --explain to see which wheels were rejected and why.
```

Each cell is one of:

| cell | `--json` status | meaning |
| --- | --- | --- |
| `✓` | `wheel` | a compatible wheel exists, so the install is fast |
| `● source` | `source` | no wheel, only an sdist, so it compiles at install time (needs a compiler, headers, time) |
| `✗ missing` | `missing` | no wheel and no sdist, so the install fails |
| `? unknown` | `unknown` | couldn't be looked up; the package's error says why |
| `–` | `n/a` | not installed on this target (environment markers exclude it) |
| `✓ bundled` | `bundled` | no wheel on PyPI, but Pyodide ships its own build (with `--pyodide-lock`) |

Packages that are fine everywhere are hidden; `--all` shows them. With several targets, the summary also counts the
gaps on each target that has any (`musl-aarch64  1 from source, 1 missing`). Packages that weren't checked, such as
editable, path or Git dependencies, are listed on the `skipped` line.

With `-q` / `--quiet` (or `quiet = true`), only the summary and the names of the packages with gaps are printed:

```text
3 of 5 packages have gaps: 3 will build from source.
gaps: fastlib, nativeext, puresdist
```

Clean runs without ignored packages are silent. Quiet text output can't be combined with `--explain`
(`--json --explain` and `--markdown --explain` work).

## --explain {#explain}

`--explain` lists, for each gap, the wheels that were rejected and why, closest misses first (up to 8 per target in
the text output):

```text
armlib 1.4.0 on musl-aarch64: ● source
  armlib-1.4.0-cp313-cp313-manylinux_2_17_aarch64.manylinux2014_aarch64.whl  manylinux_2_17_aarch64 is a glibc wheel; musl (Alpine) needs musllinux
```

For `uv.lock` inputs, and `poetry.lock` files older than lock-version 2.1 (which wheelgap reads as a dependency graph),
`--explain` also shows why the package is installed at all: the shortest dependency path from your project (or from a
dependency group, shown as `(dev group)`) to the gap package. With extra/group selection it's a path the selection
installs it through, not always the shortest. Paths are worked out per target, since markers can pull a package in on
one platform only.

```text
uvloop 0.21.0 on win-arm64: ● source
  via: myapp → uvicorn → uvloop
```

Other inputs have no dependency graph, so they show no path. If you think wheelgap got a package wrong, this output
(or `--json --explain`) is all a bug report needs, with no need to share your lockfile.

### conda-forge hint {#conda-forge}

!!! note "After 0.9.0"
    `--conda-forge` is not in 0.9.0.

`--conda-forge` adds a hint to `--explain`: for a package with a gap on a glibc Linux, macOS or Windows target, it asks
[anaconda.org](https://anaconda.org/conda-forge) whether conda-forge has a package with exactly the same (canonical)
name and a build of the same version that fits that target. For example, `wheelgap numpy==2.3.4 -p 3.14t -t
linux-ppc64le --explain --conda-forge` adds:

```
numpy: conda-forge has a package named numpy with 2.3.4 builds for linux-ppc64le
```

A build fits when all of these hold:

- It's on conda-forge's `main` label and not marked broken.
- It's for the target's conda subdir (`linux-64`, `linux-aarch64`, `linux-ppc64le`, `linux-riscv64`, `osx-64`,
  `osx-arm64`, `win-64`, `win-arm64`) or `noarch`.
- Its `__glibc` and `__osx` minimums hold for the target's glibc and macOS versions (`--glibc`, `--macos`);
  conda-forge's riscv64 builds need glibc 2.39, for example. A `noarch` build that depends on `__win`, `__unix`,
  `__linux`, `__osx` or `__glibc` fits only those OSes, and one that needs another virtual package (such as `__cuda`)
  never counts.
- It's for the target's Python:
  - a compiled build by its `python_abi` dependency: the same version and ABI, free-threaded or not;
  - an abi3 build (`_python_abi3_support`) for any non-free-threaded CPython its `cpython` constraint admits;
  - a `noarch` build by its `python` constraints.

  A build whose Python can't be told doesn't count.

musl, Pyodide, iOS and Android targets never match, and neither do `linux-i686`, `linux-armv7l`, `linux-s390x` and
`win32`, where conda-forge builds no current Python. With `--json --explain`, the package gets a `conda_forge` object:
`"conda_forge": {"name": "numpy", "version": "2.3.4", "builds": {"linux-ppc64le": ["linux-ppc64le"]}}`, keyed by target
label like `rejected`, with the subdirs of the builds that fit.

It's only a hint, so it never changes a status, the summary or the exit code: a conda-forge build doesn't make `pip
install` work, and conda names don't always match PyPI's (PyPI's `torch` is `pytorch` there). Without the flag nothing
is looked up. With it, only names known to be on PyPI are sent to anaconda.org. Either a lockfile package's registry or
index is exactly `https://pypi.org/simple` (`uv.lock`, `pylock.toml`), or this run found the package on pypi.org and
nothing points pip or uv at another index:

- **The input names no other index.** That's package names, a requirements file with no `-i`/`--index-url`,
  `--extra-index-url`, `-f`/`--find-links` or `--no-index` option (in any form pip reads, in it or in the files it pulls
  in with `-r` and `-c`), a `pyproject.toml` with no index or sources in `[tool.uv]`, `[tool.uv.pip]`, `[tool.poetry]`,
  `[tool.pdm]` or `[tool.pixi.pypi-options]`, or a `Pipfile.lock` whose sources are all PyPI. A `-c` file that isn't
  read (a URL or `${VAR}`) or an abbreviated `--requirement`/`--constraint` option counts as naming one.
- **No `uv.toml`, `pyproject.toml` or `pdm.toml` sets an index** in the input's folder or a folder above it, up to the
  repository root (the first with a `.git`). One that can't be read counts as setting one.
- **No environment variable sets one:** `PIP_INDEX_URL`, `PIP_EXTRA_INDEX_URL`, `PIP_FIND_LINKS`, `PIP_NO_INDEX`,
  `PIP_CONFIG_FILE` (any case after `PIP_`), `UV_INDEX_URL`, `UV_DEFAULT_INDEX`, `UV_INDEX`, `UV_EXTRA_INDEX_URL`,
  `UV_FIND_LINKS`, `UV_NO_INDEX`, `UV_CONFIG_FILE` or any `UV_INDEX_*`.

User and system settings aren't read: a `pip.conf` or `pip.ini`, `~/.config/uv/uv.toml`, `/etc/uv/uv.toml` and `.netrc`.
If those point pip or uv at a private index, don't use `--conda-forge` (or set one of the variables above). Others, such
as `pdm.lock`, `poetry.lock` and `pixi.lock` packages, aren't looked up, and a note on stderr names them. A run with
`--index-url`, `index-url` or `WHEELGAP_INDEX_URL` looks nothing up, and `--offline` refuses the flag. Each request is
tried once (with a 15-second socket timeout), all of them together get 30 seconds, and the first that can't reach
anaconda.org stops the rest. Answers over 16 MB (pyarrow's is about 60 MB) are refused, so at most about 250 MB is in
use. When a lookup fails or is dropped, a note on stderr says no hint isn't a "no". Answers, including "too large" and
"no such package", are cached for 24 hours like pinned lookups (`--no-cache` skips the cache); failures aren't.


## --json {#json}

`--json` prints one object. Its shape is described by
[`wheelgap-output.schema.json`](https://github.com/wheelgap/wheelgap/blob/main/docs/wheelgap-output.schema.json)
(JSON Schema 2020-12), which matches the wheelgap version it ships with; later versions may add keys, so ignore any
you don't know. Keys are never renamed or removed.

`results`, `rejected` and `via` are all keyed by target label: just the platform (`"linux-x86_64"`) when one Python is
checked, or Python and platform (`"3.13 linux-x86_64"`) with several. The top-level `targets` list gives each target's
`label`, `python` and `platform`, so a script can look the key up instead of building it:

```python
import json

data = json.load(open("wheelgap.json"))
key = next(t["label"] for t in data["targets"] if t["python"] == "3.13" and t["platform"] == "linux-x86_64")
gaps = [p["name"] for p in data["packages"] if p["results"].get(key) in ("source", "missing")]
```

With `--json --explain`, each package gets `rejected` (`{"file", "reason"}` objects) and `via` (the dependency path),
both keyed by target label like `results`; `via` is `{}` for inputs with no dependency graph.

If an input can't be read (a missing lockfile, an unknown target, a bad `[tool.wheelgap]` value), `--json` prints an
error object on stdout instead of results, next to the usual `wheelgap: error:` line on stderr and exit code 2:

```json
{"error": {"message": "nonexist.lock: no such file", "type": "SourceError"}}
```

Command-line usage errors from argparse are still plain text on stderr.

### Every key

This table is generated from the schema when the site is built.

<!-- wheelgap-output-schema -->

## --markdown {#markdown}

`--markdown` prints the same report as Markdown, for a job summary (`wheelgap --markdown >> "$GITHUB_STEP_SUMMARY"`)
or a pull request comment. The GitHub Action writes it to the run's summary page. With `--explain`, the rejected
wheels go in a collapsed section.

```markdown
### wheelgap: 3 of 5 packages have gaps

wheelgap 0.9.0 · `uv.lock` · 5 packages · Python 3.13

| package | version | linux-aarch64 | musl-aarch64 |
| --- | --- | --- | --- |
| fastlib | 2.0.0 | ● source | ● source |
| nativeext | 0.3 | ● source | ● source |
| puresdist | 1.0 | ● source | ● source |

2 packages with wheels for every target hidden (`--all` to show).

- linux-aarch64: 3 from source
- musl-aarch64: 3 from source

3 of 5 packages have gaps: 3 will build from source.

Skipped 2 non-index packages: `demo` (editable), `gitdep` (git)
```

`--json` and `--markdown` can't be used together.

## In GitHub Actions logs

!!! note "After 0.9.0"
    This guard is not in 0.9.0.

The Actions runner treats a line that starts with `::`, or contains `##[`, as a workflow command, and a lockfile in a
pull request could put such text in a package name or version. So everything `wheelgap` and `wheelgap diff` print goes
through a guard that writes a line's leading `::` as `\u003a:` and `##[` as `\u0023#[`. In `--json` output those are
JSON escapes, so the decoded values are unchanged; in text and Markdown the escape shows. Normal output never contains
either, so it's never changed, and neither are the `::error`/`::warning` lines `--github-annotations` writes.

Output to a pipe or a file is always UTF-8, even with `PYTHONIOENCODING` set, because the runner reads the log as
UTF-8. Package names in `uv.lock`, `poetry.lock`, `pdm.lock`, `pylock.toml`, `Pipfile.lock` and the PyPI packages of
`pixi.lock` must be valid PEP 508 names: anything else is an input error (exit 2) naming the file.

## JUnit XML {#junit-xml}

`--junit PATH` also writes a JUnit XML file that GitLab, Jenkins, Azure Pipelines and CircleCI can show as a test
report, alongside whatever output mode you use. It has one testsuite per target and one testcase per package:

```xml
<testsuites>
  <testsuite name="linux-aarch64" tests="5" failures="3" skipped="0">
    <testcase name="devtool" classname="linux-aarch64" />
    <testcase name="fastlib" classname="linux-aarch64"><failure message="source">source</failure></testcase>
    ...
  </testsuite>
</testsuites>
```

- A cell that fails the run is a failure: `missing`, `unknown`, and `source` unless `--fail-on missing`.
- `n/a` cells are skipped, and so are gaps that `--ignore NAME:TARGET` accepts (message `ignored`).
- After 0.9.0, a gap on an `--advisory` target is skipped with the message `advisory: source` (or `missing`,
  `unknown`), so dashboards that mark a build unstable on failed tests don't.
- Other cells (`wheel`, `bundled`, and `source` with `--fail-on missing`) pass.
- A package locked at several versions gets the version in its testcase name; one that several inputs lock at the
  same version is one testcase, which fails if any of them fails.
