"""A seeded differential test of --suggest-targets' workflow scan against PyYAML.

Each generated workflow is read twice: by the scanner, and by PyYAML followed by the same GitHub semantics (runner
labels, matrix expansion, setup-python inputs) written out plainly below. The scanner may refuse what it can't read,
but whatever it does suggest must be what the YAML says: its targets and Pythons are a subset of the oracle's, and
equal to them when it refused nothing. The YAML is varied far more than the semantics: quoting, flow and block
collections, collections over several lines or on the line after their key, comments, block scalars and multi-line
plain scalars holding workflow-like text, quotes inside plain scalars, and non-ASCII spaces.
"""

import itertools
import random

import pytest

from wheelgap.suggest_targets import _ARCHITECTURES, _FALSE, _PYTHON_RE, _TRUE, Suggestions, read_workflow_text
from wheelgap.suggest_targets import runner_target as runner

yaml = pytest.importorskip("yaml")

LABELS = ["ubuntu-latest", "ubuntu-24.04-arm", "windows-latest", "windows-11-arm", "macos-14", "macos-13", "my-box"]
LABELS += ["windows-latest\u00a0"]  # NBSP is part of the label to YAML, so this is an unknown runner
PYTHONS = ["3.11", "3.12", "3.13", "3.14", "3.14t", "3.13.1"]
ARCHES = ["x64", "x86", "arm64", ""]
NOISE = [
    "x:'y",
    "it's",
    'a"b',
    "plain # not quoted",
    "say: 'hi'",
    "[not a list",
    "{not a map",
    "tab\there",
    "nbsp\u00a0here",
    "nbsp\u00a0# not a comment",
    "runs-on: windows-latest",
    "- uses: actions/setup-python@v6",
    "architecture: x86",
]
# Plain scalars in a flow mapping: a quote after `:` with no space is an ordinary character, not a quote.
FLOW_PLAIN = ["x", "x:y", "it", "x:'y", 'x:"y', "it's"]


def plain_ok(text):
    return (
        bool(text)
        and text[0] not in "-?:,[]{}#&*!|>'\"%@` "
        and not any(bad in text for bad in (": ", " #", "\t", "[", "]", "{", "}", ","))
        and text.lower() not in ("yes", "no", "on", "off", "y", "n", "true", "false", "null", "~")
    )


def scalar(rng, text, flow=False):
    """`text` as a YAML scalar: plain where that's safe (and reads back as a string), else quoted."""
    styles = ["single"] + (["double"] if '"' not in text and "\\" not in text else [])
    if plain_ok(text) and not (flow and any(c in text for c in ",[]{}")):
        styles += ["plain"] * 2
    style = rng.choice(styles)
    if style == "plain":
        return text
    if style == "double":
        return f'"{text}"'
    return "'" + text.replace("'", "''") + "'"


def seq(rng, items, indent, flow_only=False, raw=False):
    """A list as `key:` + this: block, one-line flow, flow over several lines, or flow on the next line."""
    pad = " " * indent
    form = rng.choice(["flow", "multi", "next"] if flow_only else ["block", "flow", "multi", "next"])
    rendered = items if raw else [scalar(rng, x, flow=True) for x in items]
    if form == "block":
        return "".join(f"\n{pad}- {r}" for r in rendered)
    if form == "flow":
        return f" [{', '.join(rendered)}]"
    if form == "multi":
        body = f",\n{pad}  ".join(rendered)
        return f" [\n{pad}  {body}{rng.choice(['', ','])}\n{pad}]"
    return f"\n{pad}  [{', '.join(rendered)}]"


def mapping_item(rng, entry, indent):
    pad = " " * indent
    if rng.random() < 0.5:
        return "{" + ", ".join(f"{k}: {scalar(rng, v, flow=True)}" for k, v in entry.items()) + "}"
    lines = [f"{k}: {scalar(rng, v)}" for k, v in entry.items()]
    return f"\n{pad}  ".join(lines)


def flow_map(rng, entry):
    return "{" + ", ".join(f"{k}: {scalar(rng, v, flow=True)}" for k, v in entry.items()) + "}"


def comment(rng):
    return rng.choice(["", "", "", "  # a comment", ' # it\'s "quoted" [x', " #\u00a0nbsp"])


def noise_step(rng, pad):
    # Kinds 5 to 7 refuse the whole file (block scalars the scan doesn't read) or aren't YAML to PyYAML (a tab), so
    # they're rarer, to leave most workflows readable.
    kind = rng.choices(range(8), weights=[6, 6, 6, 6, 6, 1, 1, 1])[0]
    text = rng.choice(NOISE)
    if kind == 0:
        return f"{pad}- run: |\n{pad}    echo hi\n{pad}    {text}\n"
    if kind == 1:  # a plain scalar over several lines, whose next line looks like something else
        return f"{pad}- run: echo building\n{pad}    {text}\n"
    if kind == 2:
        return f"{pad}- run:\n{pad}    echo next line\n"
    if kind == 5:
        return f"{pad}- run: {rng.choice(['&r', '!!str', '&r !!str'])} |\n{pad}    {text}\n{pad}    more\n"
    if kind == 6:
        return f"{pad}- run:\n{pad}    |\n{pad}    {text}\n"
    if kind == 7:
        return f"{pad}\t# a comment after a tab\n{pad}- run: echo\n"
    if kind == 3:
        return f"{pad}- name: {scalar(rng, text)}{comment(rng)}\n{pad}  run: echo\n"
    return f"{pad}- uses: actions/checkout@v5\n{pad}  with:\n{pad}    fetch-depth: {scalar(rng, text)}\n"


def env_block(rng, pad):
    if rng.random() < 0.3:
        return f"{pad}env: {{A: {rng.choice(FLOW_PLAIN)}, B: {scalar(rng, 'b', flow=True)}}}\n"
    lines = [f"{pad}env:"]
    for n, text in enumerate(rng.sample(NOISE, 3)):
        lines.append(f"{pad}  V{n}: {scalar(rng, text)}{comment(rng)}")
    if rng.random() < 0.3:
        lines.append(f'{pad}  LONG: "first\n{pad}    second # not a comment\n{pad}    third"')
    return "\n".join(lines) + "\n"


def make_job(rng):
    """(YAML text of one job, what it means)."""
    matrix = {}
    use_matrix = rng.random() < 0.7
    if use_matrix:
        matrix["os"] = rng.sample(LABELS, rng.randint(1, 3))
        matrix["python"] = rng.sample(PYTHONS, rng.randint(1, 3))
        if rng.random() < 0.4:
            matrix["arch"] = rng.sample(ARCHES, rng.randint(1, 2))
    include = []
    if use_matrix and rng.random() < 0.5:
        for _ in range(rng.randint(1, 2)):
            entry = {"os": rng.choice(LABELS)}
            if rng.random() < 0.5:
                entry["arch"] = rng.choice(ARCHES)
            if rng.random() < 0.5:
                entry["python"] = rng.choice(PYTHONS)
            include.append(entry)
    exclude = []
    if use_matrix and rng.random() < 0.3:
        exclude.append({"os": rng.choice(matrix["os"]), "python": rng.choice(matrix["python"])})
    runs_on = "${{ matrix.os }}" if use_matrix and rng.random() < 0.8 else rng.choice(LABELS)
    setups = []
    for _ in range(rng.randint(0, 2)):
        action = rng.choice(["actions/setup-python@v6", "actions/setup-python@v6", "astral-sh/setup-uv@v7"])
        inputs = {}
        if rng.random() < 0.9:
            inputs["python-version"] = (
                "${{ matrix.python }}" if use_matrix and rng.random() < 0.6 else rng.choice(PYTHONS)
            )
        if action.startswith("actions/setup-python") and rng.random() < 0.5:
            inputs["architecture"] = "${{ matrix.arch }}" if use_matrix and rng.random() < 0.5 else rng.choice(ARCHES)
        if action.startswith("actions/setup-python") and rng.random() < 0.3:
            inputs["freethreaded"] = rng.choice(["true", "false", "True"])
        setups.append((action, inputs))

    lines = [f"    runs-on: {scalar(rng, runs_on) if '$' not in runs_on else runs_on}{comment(rng)}"]
    if rng.random() < 0.5:
        lines.append(env_block(rng, "    ").rstrip("\n"))
    if use_matrix:
        lines += ["    strategy:", "      matrix:"]
        for key, values in matrix.items():
            lines.append(f"        {key}:{seq(rng, values, 8)}")
        for name, entries in (("include", include), ("exclude", exclude)):
            if entries:
                if rng.random() < 0.3:
                    lines.append(
                        f"        {name}:{seq(rng, [flow_map(rng, e) for e in entries], 8, flow_only=True, raw=True)}"
                    )
                else:
                    lines.append(f"        {name}:")
                    lines += [f"          - {mapping_item(rng, e, 10)}" for e in entries]
    lines.append("    steps:")
    steps = []
    for action, inputs in setups:
        steps.append(noise_step(rng, "      ") if rng.random() < 0.5 else "")
        step = f"      - uses: {action}\n"
        if inputs:
            step += "        with:\n"
            for key, value in inputs.items():
                shown = value if "$" in value else scalar(rng, value)
                step += f"          {key}: {shown}{comment(rng)}\n"
        steps.append(step)
    steps.append(noise_step(rng, "      "))
    text = "\n".join(lines) + "\n" + "".join(steps)
    return text


def oracle(doc):
    """What the workflow means, read by PyYAML: the targets and Pythons, as GitHub would run them."""
    targets, pythons = set(), set()
    for job in doc["jobs"].values():
        matrix = (job.get("strategy") or {}).get("matrix")
        combos = expand(matrix) if matrix else [{}]
        setups = [s for s in job.get("steps") or [] if isinstance(s, dict) and "uses" in s]
        setups = [s for s in setups if s["uses"].split("@")[0] in ("actions/setup-python", "astral-sh/setup-uv")]
        for combo in combos:
            label = subst(job["runs-on"], combo)
            target = runner(label) if isinstance(label, str) else None
            # setup-python's architecture (the runner's own when unset); setup-uv's Python is the runner's own.
            archs = [
                subst((s.get("with") or {}).get("architecture", ""), combo)
                if s["uses"].startswith("actions/setup-python")
                else ""
                for s in setups
                if s["uses"].startswith("actions/setup-python") or "python-version" in (s.get("with") or {})
            ] or [""]
            for arch in archs if target else []:
                arch = "" if arch is None else str(arch)
                mapped = target if arch == "" else _ARCHITECTURES.get((target, arch))
                if mapped:
                    targets.add(mapped)
            for s in setups:
                inputs = s.get("with") or {}
                if "python-version" not in inputs:
                    continue
                version = str(subst(inputs["python-version"], combo))
                free = inputs.get("freethreaded", "false") if s["uses"].startswith("actions/setup-python") else "false"
                free = str(free).lower() if isinstance(free, bool) else str(free)
                if not (m := _PYTHON_RE.fullmatch(version)) or free not in _TRUE + _FALSE:
                    continue
                python = f"3.{m.group(1)}{m.group(2) or ('t' if free in _TRUE else '')}"
                if not python.endswith("t") or int(m.group(1)) >= 13:
                    pythons.add(python)
    return targets, pythons


def subst(value, combo):
    if isinstance(value, str) and value.startswith("${{"):
        return combo.get(value.removeprefix("${{ matrix.").removesuffix(" }}"), "")
    return value


def expand(matrix):
    """GitHub's matrix: the product, minus excludes, then each include added or appended."""
    dims = {k: v for k, v in matrix.items() if k not in ("include", "exclude")}
    combos = [dict(zip(dims, values, strict=True)) for values in itertools.product(*dims.values())] if dims else []
    combos = [c for c in combos if not any(all(c.get(k) == v for k, v in e.items()) for e in matrix.get("exclude", []))]
    extra = []
    for entry in matrix.get("include", []):
        matching = [c for c in combos if all(k not in dims or c[k] == v for k, v in entry.items())]
        for c in matching:
            c.update(entry)
        if not matching:
            extra.append(dict(entry))
    return combos + extra


def test_scan_never_disagrees_with_pyyaml():
    rng = random.Random(228)
    checked = refused_all = exact = 0
    for case in range(3000):
        jobs = [make_job(rng) for _ in range(rng.randint(1, 2))]
        head = rng.choice(["", "---\n", "%YAML 1.2\n---\n", "# workflow\n"])
        text = head + "on: push\njobs:\n" + "".join(f"  job{n}:\n{job}" for n, job in enumerate(jobs))
        if rng.random() < 0.05:  # a line break other than \n, which YAML 1.1 (PyYAML) and 1.2 read differently
            at = rng.choice([i for i, c in enumerate(text) if c == "\n"])
            text = text[:at] + rng.choice(["\r", "\x85", "\u2028", "\u2029"]) + text[at:]
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError:
            continue
        expected_targets, expected_pythons = oracle(doc)
        out = Suggestions()
        read_workflow_text(text, "ci.yml", out)
        targets, pythons = {f.value for f in out.targets}, {f.value for f in out.pythons}
        context = f"case {case}:\n{text}\nscan: {targets} {pythons} {out.unreadable}\n"
        context += f"pyyaml: {expected_targets} {expected_pythons}"
        assert targets <= expected_targets, context
        assert pythons <= expected_pythons, context
        if not out.unreadable:
            assert (targets, pythons) == (expected_targets, expected_pythons), context
            exact += 1
        checked += 1
        refused_all += not (targets or pythons) and bool(out.unreadable)
    print(f"checked {checked}, nothing read {refused_all}, exact {exact}")
    assert checked > 1500  # most generated workflows are valid YAML (tabs and stray text make some invalid)
    assert exact > checked / 5  # and the scan reads many of them in full
