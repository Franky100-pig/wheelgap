# AGENTS.md

Instructions for coding agents (Codex, Claude Code, Copilot, Cursor and others) and anyone else working on wheelgap. [CONTRIBUTING.md](CONTRIBUTING.md) has the same rules for people, in more words.

## What wheelgap does

For each package in a lockfile and each target platform, wheelgap decides whether pip or uv would install a prebuilt wheel, build from source, or fail. **A wrong answer is the worst bug.** Where wheelgap can't be sure, it must give an error (or `unknown`), never a guess.

## Setup and checks

You need [uv](https://docs.astral.sh/uv/). Run all four before you push; CI runs the same on Linux, macOS and Windows, Python 3.11–3.14 and 3.14t:

```bash
uv run --group dev ruff format .
uv run --group dev ruff check .
uv run --group dev mypy
uv run --group dev pytest
```

Try a change by hand with `uv run wheelgap numpy -t all` or `uv run wheelgap tests/fixtures/uvproj -p 3.13 -t all --explain`.

## Code map (`src/wheelgap/`)

| File | What it does |
| --- | --- |
| `cli.py` | Command line: arguments, `[tool.wheelgap]` config, text / `--json` / `--markdown` output, exit codes |
| `api.py` | Python API (`make_targets`, `check_package(s)`, `check_lockfile`) |
| `sources.py` | Reads inputs: `uv.lock`, `pylock.toml`, `poetry.lock`, `pdm.lock`, `Pipfile.lock`, requirements files |
| `targets.py` | Targets (Python × platform) and the wheel tags each accepts, via `packaging.tags` |
| `check.py` | Decides wheel / source / missing / unknown per package and target; `--explain` reasons |
| `pypi.py` | All network access: PyPI JSON lookups (`fetch_json`, `fill`) |
| `dockerfile.py` | `--dockerfile`: reads the final stage's image and platform the way Docker/BuildKit does |
| `pyodide.py` | `--pyodide-lock`: packages Pyodide ships itself |
| `scan.py` | `python -m wheelgap.scan`: the data behind the [coverage tracker](https://wheelgap.github.io/tracker/) |

## Tests

- **No network, ever.** Use the `fake_pypi` fixture in `tests/conftest.py`. To add a package, add it to `FAKE_INDEX`. The fixture returns the list of URLs fetched, so tests can assert what was (or wasn't) looked up. Lockfile fixtures live in `tests/fixtures/`; small one-off lockfiles go in `tmp_path`.
- **Every behaviour change gets a test that fails without the change.** Check this by reverting your source change and running the test.
- **Write test files with `encoding="utf-8"`** (or `write_bytes`). Windows CI otherwise writes cp1252.
- Put new cases in the existing parametrized tables where one fits, e.g. `test_dockerfiles_docker_rejects` and `test_parse_from` in `tests/test_dockerfile.py`.

## House rules

- **Dependencies:** standard library and `packaging` only.
- **Network access** only in `pypi.py`.
- **Wheel tags:** use `packaging.tags` and mirror what pip accepts; don't hand-roll tag logic.
- **`--dockerfile` must match Docker exactly or raise `DockerfileError`.** When you port BuildKit behaviour, link the file and commit in a comment (see `_heredocs` in `dockerfile.py`).
- **Output:** `--json` keys are a public interface. Add keys; don't rename or remove them. Notes go to stderr, never into `--json` or `--markdown` stdout.
- **Style:** match the surrounding code; ruff's line length is 120. Don't reformat or refactor code your change doesn't need.
- **`CHANGELOG.md`:** add one bullet at the top of `## Unreleased`, written for users, ending with the **PR** number and your handle: `(#123, thanks [@you](https://github.com/you))`. Docs-only changes get no entry.
- **README:** update it when users would notice the change (a new flag, target, input format or config key).

## Picking work and opening a PR

- Issues labelled `good first issue` say which files to change and which tests to add. Do the **first step** an issue describes; the "Later" items are separate PRs.
- **The first PR opened for an issue is the one we merge.** A comment doesn't reserve an issue, so check for an existing PR before you start, and open yours early (a draft is fine).
- In the PR description, use `Closes #N` only if the PR finishes the issue; otherwise write `Part of #N`.
- The maintainer may push fixes to your branch before merging, so leave "Allow edits by maintainers" on.
- Changes to `.github/`, `action.yml`, `pyproject.toml`, `uv.lock` or `src/wheelgap/pypi.py` are always reviewed by hand, so expect them to take longer.
