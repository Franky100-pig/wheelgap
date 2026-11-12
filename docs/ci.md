# CI

Run wheelgap in CI so a dependency bump that drops a platform's wheels fails the pull request, not the release build.
Always pass `-p` and `-t` (or set them in [`[tool.wheelgap]`](configuration.md)): otherwise the CI runner's own
Python is checked.

## GitHub Action

```yaml
- uses: actions/checkout@v7
- uses: wheelgap/wheelgap@v0.9.0
  with:
    args: uv.lock -p 3.13 -t linux-x86_64,linux-aarch64
```

The job fails when a dependency would build from source on a target you ship, for example when a dependency bump
drops ARM wheels. The gap table also goes to the run's summary page.

| input | default | meaning |
| --- | --- | --- |
| `args` | `""` | the arguments passed to wheelgap, as on the [command line](cli.md) |
| `summary` | `"true"` | also write the gap table to the job summary page (skipped when `args` include `--json`); `"false"` turns it off |

The step's result is wheelgap's exit code: [`--fail-on missing`](cli.md#fail-on), [`--ignore`](cli.md#ignore) and
[`--exit-zero`](cli.md#exit-zero) in `args` work as they do on the command line.

To compare the lockfile with the base branch's instead, put a [`diff`](diff.md) command in `args` and set
`summary: "false"`. The summary is a second run that puts `--markdown` and other flags before `args`, so with a `diff`
command it would check a PyPI package named `diff` instead:

```yaml
- uses: actions/checkout@v7
  with:
    fetch-depth: 0  # so git can read the base branch's lockfile
- uses: wheelgap/wheelgap@v0.9.0
  with:
    args: diff origin/${{ github.base_ref }}:uv.lock uv.lock -p 3.13 -t linux-x86_64,linux-aarch64
    summary: "false"
```

### Annotations {#annotations}

!!! note "After 0.9.0"
    The annotations need the Action from `main` or a release after 0.9.0: `wheelgap/wheelgap@v0.9.0` doesn't add
    them.

Each package with a gap is also marked on its own line of the lockfile in the pull request's diff, as a GitHub
annotation: an error when it fails the check, or a warning for a source build that `--fail-on missing` allows or a
gap only on `--advisory` targets. So a bump that drops a platform is flagged next to the line that bumped it.

The Action passes `--github-annotations` for this; in your own workflow steps, pass it yourself. It respects
`--ignore`, and `--exit-zero` still annotates. The annotations are printed before the usual output, or to stderr with
`--json` and `--markdown` so their stdout stays the same. GitHub shows at most 10 errors and 10 warnings per step, so
the tenth one says how many more packages have gaps. Paths are relative to `GITHUB_WORKSPACE` (or the current
directory outside Actions). The line is found in `uv.lock`, `poetry.lock`, `pdm.lock`, `pylock.toml`,
`Pipfile.lock` and requirements files (and files they include with `-r`); when wheelgap can't be sure which line a
package is on (a name and version written twice, or a `pyproject.toml` input), the annotation names the file without
a line.

### Watching a platform

!!! note "After 0.9.0"
    `--advisory` is not in 0.9.0.

To watch a platform before you support it, put `--advisory` in `args`:
`args: uv.lock -t linux-x86_64,win-arm64 --advisory win-arm64`. Its gaps show on the summary page, marked as
advisory, but don't fail the job. See [`--advisory`](cli.md#advisory).

### Your own workflow steps

Outside the Action, run wheelgap with `pipx run` (installed on GitHub's hosted runners) or `uvx`, and write the
Markdown report to the summary page:

```yaml
- run: |
    set -o pipefail
    pipx run --spec wheelgap==0.9.0 wheelgap uv.lock -p 3.13 -t linux-x86_64,linux-aarch64 --markdown | tee -a "$GITHUB_STEP_SUMMARY"
```

## pre-commit

```yaml
repos:
  - repo: https://github.com/wheelgap/wheelgap
    rev: v0.9.0
    hooks:
      - id: wheelgap
```

The hook runs when `uv.lock`, `poetry.lock`, `pdm.lock`, `pylock.toml`, `Pipfile.lock`, or a `requirements*.txt` or
`requirements*.lock` file changes, and after 0.9.0 also `pixi.lock`. Put your targets in `[tool.wheelgap]` in
`pyproject.toml` so the hook checks the right platforms. Pass extra options with `args:`, for example
`args: [--fail-on, missing]`.

## GitLab CI

GitLab shows a JUnit report in the merge request. `--junit PATH` writes one alongside the usual output, with one
testsuite per target and a failed test per gap:

```yaml
wheelgap:
  image: ghcr.io/astral-sh/uv:python3.13-bookworm-slim
  script:
    - uvx wheelgap@0.9.0 uv.lock -p 3.13 -t linux-x86_64,linux-aarch64 --junit wheelgap-junit.xml
  artifacts:
    when: always
    reports:
      junit: wheelgap-junit.xml
```

`when: always` keeps the report when the job fails, which is when you want it. See [JUnit XML](output.md#junit-xml)
for how cells map to test results.

## Other CI systems

- **Jenkins, Azure Pipelines, CircleCI:** publish the `--junit PATH` file with their JUnit test report step.
- **Any CI:** the [exit code](output.md#exit-codes) fails the job; `--json` gives a machine-readable report and
  `--markdown` one you can post as a comment.
- **Monorepos and multi-arch images:** see the [recipes](recipes.md).
