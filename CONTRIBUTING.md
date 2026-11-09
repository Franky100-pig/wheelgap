# Contributing

Thanks for helping! wheelgap is deliberately small: one job, one dependency (`packaging`). First contributions are very welcome, and nobody expects you to know the codebase.

wheelgap is maintained by [@patrickballard](https://github.com/patrickballard), who reviews and merges pull requests.

## Setup (3 commands)

You need [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/wheelgap/wheelgap && cd wheelgap
uv run --group dev pytest        # tests
uv run --group dev ruff check .  # lint
uv run wheelgap numpy -t all     # try it
```

## Picking something to work on

- Issues labelled [`good first issue`](https://github.com/wheelgap/wheelgap/labels/good%20first%20issue) are scoped to be doable in an evening, and each one says which files to touch and what to test. [`help wanted`](https://github.com/wheelgap/wheelgap/labels/help%20wanted) issues are bigger.
- **The first PR wins.** Comments like "I'd like to take this" are welcome, but they don't reserve an issue: the first pull request opened for an issue is the one we review and merge. So open yours early (a draft is fine), and check the issue for an existing PR before you start.
- If there's no PR after about two weeks, the issue is free for someone else. Life happens, and that's fine; just comment if you're still on it.
- **Want to add something that isn't an issue yet?** Post it in [Discussions → Ideas](https://github.com/wheelgap/wheelgap/discussions/categories/ideas) first, so we can agree it fits before you spend time on it. Small fixes (typos, docs, obvious bugs) can go straight to a PR.

## Making a pull request

1. Fork the repo and create a branch.
2. Make your change. Before pushing, run:
   ```bash
   uv run --group dev ruff format .
   uv run --group dev ruff check .
   uv run --group dev mypy
   uv run --group dev pytest
   ```
3. Open a PR and put `Closes #123` in the description, so the issue closes when it's merged.

The PR template has a short checklist:

- **Tests** for any behaviour change. Tests never hit the network: use the `fake_pypi` fixture in `tests/conftest.py`, or a lockfile fixture under `tests/fixtures/`.
- **A line in `CHANGELOG.md`** under `## Unreleased`, written for users.
- **README updates** if users would notice the change (a new flag, target or input format).
- Keep the code in the same style as the surrounding code. `ruff` handles formatting.

## What to expect

- A review within a day or so. Most PRs need a small change or two, which is normal.
- PRs are squash-merged, so don't worry about tidying your commit history.
- You'll be credited in the release notes.

## AI-assisted contributions

Using AI tools is fine. Please make sure you've run the code and the tests yourself and can explain what the change does, the same as for anything you write by hand.

## Reporting a wrong result

Found a package that wheelgap misjudges? Open a [bug report](https://github.com/wheelgap/wheelgap/issues/new?template=bug_report.yml) with the output of the same command plus `--explain`. It lists the wheel filenames and why each was rejected, which is everything needed to write a test, so you don't have to share your lockfile.

## Questions

Ask in [Discussions → Q&A](https://github.com/wheelgap/wheelgap/discussions/categories/q-a). No question is too basic.

Everyone taking part is expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
