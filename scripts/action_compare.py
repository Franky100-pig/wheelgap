"""Turn `wheelgap diff --json` into the GitHub Action's job summary and PR comment (`compare-to`, #201).

    python3 scripts/action_compare.py --diff-json diff.json --exit-code 1 --lockfile uv.lock \\
        --base 1a2b3c4 --base-branch --markdown report.md --payload comment.json

The Action runs this with the runner's own Python, not wheelgap's, so it uses only the standard library and
doesn't import wheelgap. It writes the Markdown report (for $GITHUB_STEP_SUMMARY) and, with --payload, the JSON
body for GitHub's comment API: the same report under a hidden marker that names the lockfile, so each lockfile
keeps one comment that later runs update.

Package names, versions and wheelgap's messages come from the pull request, so they never reach the log (stdout
gets one line built from counts) and are escaped in the report: every character but ASCII letters, digits,
spaces and `-` becomes an HTML character reference, inside `<code>` in a raw HTML block. Markdown doesn't parse
an HTML block, so nothing can become a table cell, a link or emphasis, and GitHub doesn't turn text inside
`<code>` into @mentions or issue links.
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from urllib.parse import quote

# As wheelgap's own text output shows each status (check.CELL; a test keeps the two in step).
CELLS = {
    "wheel": "✓",
    "source": "● source",
    "missing": "✗ missing",
    "unknown": "? unknown",
    "n/a": "–",
    "bundled": "✓ bundled",
}
MAX_TEXT = 100  # characters of one name, version or message, before escaping
MAX_BODY = 60_000  # GitHub refuses comments over 65,536 characters
MARKER = "<!-- wheelgap-compare lockfile={} -->"


def esc(text: object, limit: int = MAX_TEXT) -> str:
    """`text` as inert HTML: letters, digits, spaces and `-` as they are, everything else as `&#N;`. Control and
    format characters (line breaks, bidi overrides) are shown as `\\uXXXX` instead, so they can't reorder or
    split what's around them."""
    s = str(text)
    if len(s) > limit:
        s = s[:limit] + "…"
    out = []
    for ch in s:
        if ch.isascii() and (ch.isalnum() or ch in " -"):
            out.append(ch)
        elif unicodedata.category(ch).startswith("C"):
            out.append("".join(f"&#{ord(c)};" for c in f"\\u{ord(ch):04x}"))
        else:
            out.append(f"&#{ord(ch)};")
    return "".join(out)


def code(text: object) -> str:
    return f"<code>{esc(text)}</code>"


def versions(vs: object) -> str:
    if not isinstance(vs, list) or not vs:
        return "?"
    return ", ".join(code(v) for v in vs)


def cell(side: dict, prefix: str = "") -> str:
    status = side.get(f"{prefix}status")
    if status == "skipped":
        return f"skipped ({esc(side.get(f'{prefix}skip_reason', '?'))})"
    if isinstance(status, str) and status in CELLS:
        return CELLS[status]
    return code(status)


def rows(data: dict) -> list[str]:
    """One table row per package and target that got worse, then the new packages' gaps."""
    out = []
    for pkg in data["worse"]:
        old, new = versions(pkg.get("old_versions")), versions(pkg.get("new_versions"))
        version = new if old == new else f"{old} → {new}"
        for target, side in pkg.get("targets", {}).items():
            out.append(
                f"<tr><td>{code(pkg.get('name'))}</td><td>{version}</td><td>{code(target)}</td>"
                f"<td>{cell(side, 'old_')} → {cell(side, 'new_')}</td></tr>"
            )
    for pkg in data["new"]:
        version = f"(new) {versions(pkg.get('versions'))}"
        for target, side in pkg.get("targets", {}).items():
            out.append(
                f"<tr><td>{code(pkg.get('name'))}</td><td>{version}</td><td>{code(target)}</td>"
                f"<td>(new) → {cell(side)}</td></tr>"
            )
    return out


def counts(n_worse: int, n_new: int) -> str:
    """As `wheelgap diff` words its last line."""
    if not n_worse and not n_new:
        return "Nothing got worse."
    parts = []
    if n_worse:
        parts.append(f"{n_worse} package{'s' * (n_worse != 1)} got worse")
    if n_new:
        parts.append(f"{n_new} new package{'s' * (n_new != 1)} {'has' if n_new == 1 else 'have'} gaps")
    return ", ".join(parts) + "."


def load(path: str, exit_code: int) -> tuple[dict | None, str]:
    """The diff's JSON when it compared the lockfiles, else None and why not (escaped)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None, f"<code>wheelgap diff</code> exited {int(exit_code)} without a report; the log says why."
    if not isinstance(data, dict):
        return None, "<code>wheelgap diff</code> printed something other than its report."
    if isinstance(data.get("error"), dict):
        error = data["error"]
        return None, f"<code>wheelgap diff</code> couldn't compare them: {code(error.get('message', '?'))}"
    if exit_code not in (0, 1) or not isinstance(data.get("worse"), list) or not isinstance(data.get("new"), list):
        return None, f"<code>wheelgap diff</code> exited {int(exit_code)}; the log says why."
    return data, ""


def report(data: dict | None, problem: str, lockfile: str, base: str, base_branch: bool = False) -> str:
    lines = ["### wheelgap diff", ""]
    compared = f"Compared {code(lockfile)} with " + (
        f"the base branch ({code(base)})." if base_branch else f"{code(base)}."
    )
    if data is None:
        return "\n".join([*lines, f"<p>{compared} {problem}</p>", ""])
    n_worse, n_new = len(data["worse"]), len(data["new"])
    lines.append(f"<p>{compared} {counts(n_worse, n_new)}</p>")
    table = rows(data)
    if table:
        lines += ["", "<table>", "<tr><th>Package</th><th>Version</th><th>Target</th><th>Status</th></tr>"]
        size = sum(len(line) for line in lines)
        shown = 0
        for row in table:
            if size + len(row) > MAX_BODY - 1_000:
                break
            lines.append(row)
            size += len(row) + 1
            shown += 1
        if shown < len(table):
            more = len(table) - shown
            lines.append(f'<tr><td colspan="4">and {more} more row{"s" * (more != 1)}</td></tr>')
        lines.append("</table>")
    unchecked = data.get("unchecked")
    if isinstance(unchecked, list) and unchecked:
        names = ", ".join(code(n) for n in unchecked[:20]) + (", …" if len(unchecked) > 20 else "")
        lines += ["", f"<p>Couldn't check at the same version on either side: {names}</p>"]
    lines.append("")
    return "\n".join(lines)


def marker(lockfile: str) -> str:
    """The hidden marker that finds this lockfile's comment again: the path percent-encoded, `-` included, so
    it can't close the HTML comment."""
    return MARKER.format(quote(lockfile, safe="/._").replace("-", "%2D"))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--diff-json", required=True, help="what `wheelgap diff --json` printed")
    p.add_argument("--exit-code", type=int, required=True, help="wheelgap diff's exit code")
    p.add_argument("--lockfile", required=True)
    p.add_argument("--base", required=True, help="the git ref or commit the lockfile was compared with")
    p.add_argument("--base-branch", action="store_true", help="--base is the pull request's base commit")
    p.add_argument("--markdown", required=True, help="write the report here")
    p.add_argument("--payload", help="also write the comment API's JSON body here")
    p.add_argument("--marker", help="also write the comment's marker here")
    args = p.parse_args(argv)
    data, problem = load(args.diff_json, args.exit_code)
    text = report(data, problem, args.lockfile, args.base, args.base_branch)
    with open(args.markdown, "w", encoding="utf-8") as f:
        f.write(text)
    if args.payload:
        with open(args.payload, "w", encoding="utf-8") as f:
            json.dump({"body": f"{marker(args.lockfile)}\n{text}"}, f)
    if args.marker:
        with open(args.marker, "w", encoding="utf-8") as f:
            f.write(marker(args.lockfile))
    # Counts only: names and versions in the log could be read as workflow commands.
    if data is None:
        print("wheelgap: couldn't compare the lockfiles; the job summary and the log above say why.")
    else:
        print(f"wheelgap: {counts(len(data['worse']), len(data['new']))} The job summary has the details.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
