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
