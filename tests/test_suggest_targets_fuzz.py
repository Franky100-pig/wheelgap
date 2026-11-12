"""Seeded differential tests of --suggest-targets' workflow scan against PyYAML.

Each generated workflow is read twice: by the scanner, and by PyYAML (`yaml.compose`, so plain and quoted scalars stay
apart) followed by GitHub's semantics written out plainly below, with their own runner and architecture tables rather
than the module's. The scanner may refuse what it can't read, but whatever it suggests must be what the YAML says: its
targets and Pythons are a subset of the oracle's, and equal to them when it refused nothing.

Three generators: realistic workflows with varied YAML (`realistic`), random atoms of YAML syntax around and inside
real values (`atoms`), and junk values next to a fixed 32-bit setup step that a misread would turn into win-amd64
(`junk`). The last two came from the PR's review.
"""

import itertools
import random
import re

import pytest

from wheelgap.suggest_targets import Suggestions, read_workflow_text

yaml = pytest.importorskip("yaml")

# The labels the generators use, with the target each runs on (GitHub's runner list), and setup-python's
# architectures where they're certain.
RUNNERS = {
    "ubuntu-latest": "linux-x86_64",
    "ubuntu-24.04-arm": "linux-aarch64",
    "windows-latest": "win-amd64",
    "windows-11-arm": "win-arm64",
    "macos-latest": "macos-arm64",
    "macos-14": "macos-arm64",
    "macos-13": "macos-x86_64",
    "macos-15-intel": "macos-x86_64",
}
ARCH = {
    ("linux-x86_64", "x64"): "linux-x86_64",
    ("linux-aarch64", "arm64"): "linux-aarch64",
    ("win-amd64", "x64"): "win-amd64",
    ("win-amd64", "x86"): "win32",
    ("win-arm64", "arm64"): "win-arm64",
    ("macos-x86_64", "x64"): "macos-x86_64",
    ("macos-arm64", "arm64"): "macos-arm64",
    ("macos-arm64", "x64"): "macos-x86_64",
}
PYTHON = re.compile(r"3\.(\d+)(?:\.\d+)?(t?)")
MATRIX_REF = re.compile(r"\$\{\{[ \t]*matrix\.([A-Za-z_][A-Za-z0-9_-]*)[ \t]*\}\}")
TRUE, FALSE = ("true", "True", "TRUE"), ("false", "False", "FALSE")  # core.getBooleanInput


class Skip(Exception):
    """A workflow the oracle can't judge for sure (a merge key, a comparison GitHub may make either way)."""


class S:
    """A scalar as PyYAML read it: its text, and whether it was plain (so 3.10 is a number) or quoted."""

    def __init__(self, text, plain):
        self.text, self.plain = text, plain

    def gh(self):
        """The string GitHub hands on."""
        if self.plain and re.fullmatch(r"[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?", self.text):
            number = float(self.text)
            if number == int(number) and "." not in self.text:
                return str(int(number))
            return repr(number)
        if self.plain and self.text.lower() in ("", "~", "null"):
            return ""
        return self.text


def convert(node):
    if isinstance(node, yaml.ScalarNode):
        return S(node.value, node.style is None and node.tag != "tag:yaml.org,2002:str")
    if isinstance(node, yaml.SequenceNode):
        return [convert(n) for n in node.value]
    out = {}
    for k, v in node.value:
        if not isinstance(k, yaml.ScalarNode) or k.value in out or k.value == "<<":
            raise Skip("complex, duplicate or merge key")
        out[k.value] = convert(v)
    return out


def expand(matrix):
    """GitHub's matrix: the product, minus excludes, then each include added or appended."""
    if not isinstance(matrix, dict):
        raise Skip("matrix")
    dims = {k: v for k, v in matrix.items() if k not in ("include", "exclude")}
    if not all(isinstance(v, list) and all(isinstance(x, S) for x in v) for v in dims.values()):
        raise Skip("dims")
    include, exclude = matrix.get("include", []), matrix.get("exclude", [])
    for entries in (include, exclude):
        if not isinstance(entries, list) or not all(
            isinstance(e, dict) and all(isinstance(x, S) for x in e.values()) for e in entries
        ):
            raise Skip("include/exclude")

    def same(a, b):
        equal = a.gh() == b.gh()
        if (a.plain != b.plain or a.text != b.text) and (a.text.lower() == b.text.lower() or equal):
            if not (a.text == b.text and not re.fullmatch(r"[-+.0-9eE]+|(?i:true|false|null|~|)", a.text)):
                raise Skip("a comparison GitHub may make either way")
        return equal

    combos = [dict(zip(dims, values, strict=True)) for values in itertools.product(*dims.values())] if dims else []
    combos = [c for c in combos if not any(all([k in c and same(c[k], v) for k, v in e.items()]) for e in exclude)]
    extra = []
    for entry in include:
        matching = [c for c in combos if all([same(c[k], v) for k, v in entry.items() if k in dims])]
        for c in matching:
            c.update(entry)
        if not matching:
            extra.append(dict(entry))
    return combos + extra


def value(scalar, combo):
    """An input's or runs-on's value in a combination: a string, or None when it can't be known."""
    if not isinstance(scalar, S):
        return None
    if m := MATRIX_REF.fullmatch(scalar.text):
        v = combo.get(m.group(1))
        return "" if v is None else v.gh()
    if "${{" in scalar.text:
        return None
    return scalar.gh()


def oracle(text):
    doc = convert(yaml.compose(text, Loader=yaml.SafeLoader))
    targets, pythons = set(), set()
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
        raise Skip("jobs")
    for job in doc["jobs"].values():
        if not isinstance(job, dict):
            continue
        strategy = job.get("strategy")
        matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
        if isinstance(matrix, S):
            raise Skip("computed matrix")
        combos = expand(matrix) if matrix is not None else [{}]
        steps = job.get("steps")
        steps = (
            [] if steps is None or isinstance(steps, S) and steps.plain and steps.text in ("", "~", "null") else steps
        )
        if not isinstance(steps, list):
            continue
        setups = []
        for step in steps:
            if isinstance(step, dict) and isinstance(step.get("uses"), S):
                action = step["uses"].text.split("@")[0].lower()
                if action in ("actions/setup-python", "astral-sh/setup-uv"):
                    inputs = step.get("with") or {}
                    inputs = {k.lower(): v for k, v in inputs.items()} if isinstance(inputs, dict) else {}
                    setups.append((action == "actions/setup-python", inputs))
        for combo in combos:
            label = value(job.get("runs-on"), combo) if "container" not in job else None
            target = RUNNERS.get((label or "").lower())
            # Each step that picks an interpreter: setup-python (its architecture, the runner's own when unset or
            # ''), and setup-uv with a Python (the runner's own).
            archs = [
                (value(i["architecture"], combo) if "architecture" in i else "") if is_python else ""
                for is_python, i in setups
                if is_python or "python-version" in i or "python-arch" in i
            ] or [""]
            for arch in archs:
                if target and arch is not None:
                    mapped = target if arch == "" else ARCH.get((target, arch))
                    if mapped:
                        targets.add(mapped)
            for is_python, inputs in setups:
                if "python-version" not in inputs:
                    continue
                version = value(inputs["python-version"], combo)
                free = value(inputs["freethreaded"], combo) if is_python and "freethreaded" in inputs else "false"
                if not version or not (m := PYTHON.fullmatch(version)) or free not in TRUE + FALSE:
                    continue
                t = m.group(2) or ("t" if free in TRUE else "")
                if not (t and int(m.group(1)) < 13):
                    pythons.add(f"3.{m.group(1)}{t}")
    return targets, pythons


# --- realistic: real workflows with varied YAML ---

LABELS = [*RUNNERS, "my-box", "windows-latest\u00a0"]  # NBSP is part of the label to YAML: an unknown runner
VERSIONS = ["3.11", "3.12", "3.13", "3.14", "3.14t", "3.13.1", "3.10", "pypy3.10"]
ARCHES = ["x64", "x86", "arm64", ""]
NOISE = [
    "x:'y",
    "it's",
    'a"b',
    "plain # not quoted",
    "[not a list",
    "{not a map",
    "nbsp\u00a0here",
    "nbsp\u00a0# not a comment",
    "runs-on windows-latest",
    "architecture x86",
]


def plain_ok(text):
    return (
        bool(text)
        and text[0] not in "-?:,[]{}#&*!|>'\"%@` "
        and not any(bad in text for bad in (": ", " #", "\t", "[", "]", "{", "}", ","))
        and text.lower() not in ("yes", "no", "on", "off", "y", "n", "true", "false", "null", "~")
    )


def scalar(rng, text, flow=False):
    styles = ["single"] + (["double"] if '"' not in text and "\\" not in text else [])
    if plain_ok(text) and not (flow and any(c in text for c in ",[]{}")):
        styles += ["plain"] * 2
    style = rng.choice(styles)
    if style == "plain":
        return text
    if style == "double":
        return f'"{text}"'
    return "'" + text.replace("'", "''") + "'"


def comment(rng):
    return rng.choice(["", "", "", "  # a comment", ' # it\'s "quoted" [x', " #\u00a0nbsp"])


def seq(rng, items, indent, raw=False, flow_only=False):
    pad = " " * indent
    rendered = items if raw else [scalar(rng, x, flow=True) for x in items]
    form = rng.choice(["flow", "multi", "next"] if flow_only else ["block", "flow", "multi", "next"])
    if form == "block":
        return "".join(f"\n{pad}- {r}" for r in rendered)
    if form == "flow":
        return f" [{', '.join(rendered)}]"
    if form == "multi":
        body = f",\n{pad}  ".join(rendered)
        return f" [\n{pad}  {body}{rng.choice(['', ','])}\n{pad}]"
    return f"\n{pad}  [\n" + "".join(f"{pad}    {r},\n" for r in rendered) + f"{pad}  ]"


def flow_map(rng, entry):
    return "{" + ", ".join(f"{k}: {scalar(rng, v, flow=True)}" for k, v in entry.items()) + "}"


def noise_step(rng, pad):
    text = rng.choice(NOISE)
    kind = rng.choices(range(7), weights=[6, 6, 6, 6, 6, 1, 1])[0]
    if kind == 0:
        return f"{pad}- run: |\n{pad}    echo hi\n{pad}    {text}\n"
    if kind == 1:
        return f"{pad}- run: echo building\n{pad}    {text}\n"
    if kind == 2:
        return f"{pad}- run:\n{pad}    echo next line\n"
    if kind == 3:
        return f"{pad}- name: {scalar(rng, text)}{comment(rng)}\n{pad}  run: echo\n"
    if kind == 4:
        return f"{pad}- uses: actions/checkout@v5\n{pad}  with:\n{pad}    fetch-depth: {scalar(rng, text)}\n"
    if kind == 5:
        return f"{pad}- run: {rng.choice(['&r', '!!str'])} |\n{pad}    {text}\n"
    return f"{pad}\t# a comment after a tab\n{pad}- run: echo\n"


def realistic_job(rng):
    use_matrix = rng.random() < 0.7
    matrix, include, exclude = {}, [], []
    if use_matrix:
        matrix["os"] = rng.sample(LABELS, rng.randint(1, 3))
        matrix["python"] = rng.sample(VERSIONS, rng.randint(1, 3))
        if rng.random() < 0.4:
            matrix["arch"] = rng.sample(ARCHES, rng.randint(1, 2))
        for _ in range(rng.choice([0, 0, 1, 2])):
            entry = {"os": rng.choice(LABELS)}
            for key, pool in (("arch", ARCHES), ("python", VERSIONS)):
                if rng.random() < 0.5:
                    entry[key] = rng.choice(pool)
            include.append(entry)
        if rng.random() < 0.3:
            exclude.append({"os": rng.choice(matrix["os"]), "python": rng.choice(matrix["python"])})
    runs_on = "${{ matrix.os }}" if use_matrix and rng.random() < 0.8 else rng.choice(LABELS)
    lines = [f"    runs-on: {runs_on if '$' in runs_on else scalar(rng, runs_on)}{comment(rng)}"]
    if rng.random() < 0.4:
        lines.append("    env:")
        lines += [f"      V{n}: {scalar(rng, text)}{comment(rng)}" for n, text in enumerate(rng.sample(NOISE, 3))]
    if use_matrix:
        lines += ["    strategy:", "      matrix:"]
        lines += [f"        {key}:{seq(rng, values, 8)}" for key, values in matrix.items()]
        for name, entries in (("include", include), ("exclude", exclude)):
            if entries and rng.random() < 0.3:
                lines.append(
                    f"        {name}:{seq(rng, [flow_map(rng, e) for e in entries], 8, raw=True, flow_only=True)}"
                )
            elif entries:
                lines.append(f"        {name}:")
                for e in entries:
                    first, *rest = e.items()
                    lines.append(f"          - {first[0]}: {scalar(rng, first[1])}")
                    lines += [f"            {k}: {scalar(rng, v)}" for k, v in rest]
    lines.append("    steps:")
    steps = []
    for _ in range(rng.randint(0, 2)):
        action = rng.choice(["actions/setup-python@v6", "actions/setup-python@v6", "astral-sh/setup-uv@v7"])
        inputs = {}
        if rng.random() < 0.9:
            inputs["python-version"] = (
                "${{ matrix.python }}" if use_matrix and rng.random() < 0.6 else rng.choice(VERSIONS)
            )
        if "python" in action and rng.random() < 0.5:
            inputs["architecture"] = "${{ matrix.arch }}" if use_matrix and rng.random() < 0.5 else rng.choice(ARCHES)
        if "python" in action and rng.random() < 0.3:
            inputs["freethreaded"] = rng.choice(["true", "false", "True"])
        if rng.random() < 0.5:
            steps.append(noise_step(rng, "      "))
        step = f"      - uses: {action}\n"
        if inputs:
            step += "        with:\n" + "".join(
                f"          {k}: {v if '$' in v else scalar(rng, v)}{comment(rng)}\n" for k, v in inputs.items()
            )
        steps.append(step)
    steps.append(noise_step(rng, "      "))
    return "\n".join(lines) + "\n" + "".join(steps)


def realistic(rng):
    jobs = [realistic_job(rng) for _ in range(rng.randint(1, 2))]
    head = rng.choice(["", "---\n", "%YAML 1.2\n---\n", "# workflow\n"])
    text = head + "on: push\njobs:\n" + "".join(f"  job{n}:\n{job}" for n, job in enumerate(jobs))
    if rng.random() < 0.05:  # a line break other than \n
        at = rng.choice([i for i, c in enumerate(text) if c == "\n"])
        text = text[:at] + rng.choice(["\r", "\x85", "\u2028", "\u2029"]) + text[at:]
    return text


# --- atoms: random pieces of YAML syntax around and inside real values ---

ATOMS = [
    "a", "b", "x86", "x64", "it's", "x:'y", 'x:"y', "- ", " - ", "'", '"', "'", '"', " #", "#", " # c", "[", "]", "{",
    "}", ",", ", ", ": ", ":", " ", "''", '\\"', "\\", "!!str ", "&a ", "*a", "}}", "${{ x }}", "\u00a0", "\t", "? ",
    "|", ">", "windows-latest", "architecture: x86", "@", "`", "%", "=", "- uses: actions/setup-python@v5", "!x ",
]  # fmt: skip


def junk(rng, low=1, high=4):
    return "".join(rng.choice(ATOMS) for _ in range(rng.randint(low, high)))


def quoted_or_plain(rng, text, flow=False):
    safe = (
        text
        and not re.search(r"^[-?:,\[\]{}#&*!|>'\"%@` ]|: | #|\$", text)
        and not (flow and re.search(r"[,\[\]{}]", text))
    )
    if "${{" in text and not flow:
        safe = True
    c = rng.random()
    if safe and c < 0.45:
        return text
    if c < 0.75 or "\\" in text or '"' in text:
        return "'" + text.replace("'", "''") + "'"
    return '"' + text + '"'


def atom_comment(rng):
    return rng.choice(["", "", "", " # c", " #c 'x", ' # "', " # [", " # }", "\t# t", " #\u00a0"])


def flow_noise(rng, pad):
    items = []
    kind = rng.choice("smjJ")
    for i in range(rng.randint(1, 3)):
        v = junk(rng)
        if kind == "s":
            items.append(v)
        elif kind == "m":
            items.append(f"K{i}: {v}")
        elif kind == "j":
            items.append(f'"K{i}":{v}')
        else:
            items.append(f"K{i}:{v}" if rng.random() < 0.3 else f"{rng.choice(['? ', '&k ', '!!str ', ''])}K{i} : {v}")
    o, c = ("[", "]") if kind == "s" else ("{", "}")
    layout = rng.choice(["one", "one", "multi", "next", "nextmulti", "lead"])
    ind = pad + "  " if rng.random() < 0.8 else pad[: rng.randint(0, len(pad))]
    if layout == "one":
        return f" {o}{', '.join(items)}{c}{atom_comment(rng)}"
    if layout == "multi":
        body = "".join(f"{ind}{it},{atom_comment(rng)}\n" for it in items).rstrip("\n").rstrip(",")
        return f" {o}{atom_comment(rng)}\n{body}\n{ind}{c}{atom_comment(rng)}"
    if layout == "next":
        return f"\n{pad}  {o}{', '.join(items)}{c}{atom_comment(rng)}"
    if layout == "nextmulti":
        return f"\n{pad}  {o}\n" + "".join(f"{pad}    {it},\n" for it in items) + f"{pad}  {c}"
    return f" {o}{items[0]}" + "".join(f"\n{ind}, {it}" for it in items[1:]) + f"\n{ind}{c}"


def noise_kv(rng, pad, key):
    kind = rng.randrange(9)
    if kind == 0:
        return f"{pad}{key}:{flow_noise(rng, pad)}\n"
    if kind == 1:
        return f"{pad}{key}: {junk(rng)}{atom_comment(rng)}\n"
    if kind == 2:  # a plain scalar over several lines
        more = "".join(
            f"{pad}{' ' * rng.randint(1, 4)}{junk(rng)}{atom_comment(rng)}\n" for _ in range(rng.randint(1, 3))
        )
        return f"{pad}{key}: a{junk(rng, 0, 2)}\n" + more
    if kind == 3:  # a quoted scalar over several lines
        q = rng.choice("'\"")
        ind = pad + "  " if rng.random() < 0.7 else pad[: rng.randint(0, len(pad))]
        body = junk(rng, 0, 3).replace(q, "")
        start = f"{rng.choice(['', '', '&a ', '!!str '])}{q}a{junk(rng, 0, 2).replace(q, '')}"
        return f"{pad}{key}: {start}\n{ind}{body}\n{ind}b{q}\n"
    if kind == 4:
        header = rng.choice(["|", ">", "|-", ">+", "|2", "&a |", "!!str >"])
        return f"{pad}{key}: {header}{atom_comment(rng)}\n" + "".join(
            f"{pad}    {junk(rng)}\n" for _ in range(rng.randint(1, 2))
        )
    if kind == 5:
        return f"{pad}{key}:\n" + "".join(
            f"{pad}  S{i}: {junk(rng)}{atom_comment(rng)}\n" for i in range(rng.randint(1, 2))
        )
    if kind == 6:
        return f"{pad}{key}:\n{pad}  {junk(rng)}\n"
    if kind == 7:
        return f"{pad}{key}:\n" + "".join(
            f"{pad}  - {junk(rng)}{atom_comment(rng)}\n" for _ in range(rng.randint(1, 2))
        )
    return f"{pad}{key}: {rng.choice(['&a ', '!!str ', '!t '])}{junk(rng)}\n"


def atom_list(rng, values, pad, flow_only=False):
    layout = rng.choice(
        ["flow", "multi", "next", "nextmulti", "lead", "tight"] + ([] if flow_only else ["block", "block0"])
    )
    items = [v if flow_only else quoted_or_plain(rng, v, flow=layout not in ("block", "block0")) for v in values]
    if layout == "block":
        return "".join(f"\n{pad}  - {x}{atom_comment(rng)}" for x in items)
    if layout == "block0":
        return "".join(f"\n{pad}- {x}{atom_comment(rng)}" for x in items)
    if layout == "flow":
        return f" [{', '.join(items)}]{atom_comment(rng)}"
    if layout == "tight":
        return f" [{','.join(items)}]"
    if layout == "multi":
        return f" [{atom_comment(rng)}\n" + "".join(f"{pad}  {x},{atom_comment(rng)}\n" for x in items) + f"{pad}]"
    if layout == "next":
        return f"\n{pad}  [{', '.join(items)}]{atom_comment(rng)}"
    if layout == "nextmulti":
        body = "".join(
            f"{pad}    {x}{',' if i < len(items) - 1 or rng.random() < 0.5 else ''}{atom_comment(rng)}\n"
            for i, x in enumerate(items)
        )
        return f"\n{pad}  [\n{body}{pad}  ]"
    return f" [{items[0]}" + "".join(f"\n{pad}  , {x}" for x in items[1:]) + f"\n{pad}  ]"


def atom_map(rng, entry):
    sep = rng.choice([", ", ",", " , "])
    pairs = sep.join(
        f"{quoted_or_plain(rng, k, flow=True) if rng.random() < 0.2 else k}: {quoted_or_plain(rng, v, flow=True)}"
        for k, v in entry.items()
    )
    return "{" + rng.choice(["", " "]) + pairs + rng.choice(["", " "]) + "}"


def atoms(rng):
    labels = [
        "ubuntu-latest",
        "ubuntu-24.04-arm",
        "windows-latest",
        "windows-11-arm",
        "macos-latest",
        "macos-15-intel",
        "self-hosted",
        "Windows-Latest",
    ]
    arches = ["x86", "x64", "arm64", ""]
    versions = ["3.10", "3.12", "3.13", "3.14t", "3.9", "3.13.2", "pypy3.10", "3.x"]
    out = ["on: push\n"]
    if rng.random() < 0.3:
        out.append(noise_kv(rng, "", "env"))
    out.append("jobs:\n  b:\n")
    pad = "    "
    use_matrix = rng.random() < 0.65
    dims, include, exclude = {}, [], []
    if use_matrix:
        for key, pool, chance in (("os", labels, 0.8), ("arch", arches, 0.5), ("py", versions, 0.5)):
            if rng.random() < chance:
                dims[key] = rng.sample(pool, rng.randint(1, min(3, len(pool))))
        for _ in range(rng.choice([0, 0, 1, 2])):
            entry = {
                k: rng.choice(pool)
                for k, pool in (("os", labels), ("arch", arches), ("py", versions))
                if rng.random() < 0.5
            }
            if entry:
                include.append(entry)
        if dims and rng.random() < 0.3:
            first = next(iter(dims))
            exclude.append({k: rng.choice(v) for k, v in dims.items() if rng.random() < 0.6} or {first: dims[first][0]})
    parts = []
    runs_on = "${{ matrix.os }}" if use_matrix and rng.random() < 0.7 else rng.choice(labels)
    parts.append(f"{pad}runs-on: {quoted_or_plain(rng, runs_on)}{atom_comment(rng)}\n")
    if use_matrix:
        keys = list(dims)
        rng.shuffle(keys)
        body = [f"{pad}    {k}:{atom_list(rng, dims[k], pad + '    ')}\n" for k in keys]
        for name, entries in (("include", include), ("exclude", exclude)):
            if not entries:
                continue
            if rng.random() < 0.4:
                body.append(
                    f"{pad}    {name}:"
                    f"{atom_list(rng, [atom_map(rng, e) for e in entries], pad + '    ', flow_only=True)}\n"
                )
            else:
                text = f"{pad}    {name}:\n"
                for e in entries:
                    if rng.random() < 0.3:
                        text += f"{pad}      - {atom_map(rng, e)}{atom_comment(rng)}\n"
                    else:
                        (k0, v0), *rest = e.items()
                        text += f"{pad}      - {k0}: {quoted_or_plain(rng, v0)}{atom_comment(rng)}\n"
                        text += "".join(
                            f"{pad}        {k}: {quoted_or_plain(rng, v)}{atom_comment(rng)}\n" for k, v in rest
                        )
                body.append(text)
        rng.shuffle(body)
        if rng.random() < 0.15:
            body.insert(rng.randint(0, len(body)), noise_kv(rng, pad + "    ", "zz"))
        parts.append(f"{pad}strategy:\n{pad}  matrix:\n" + "".join(body))
    for _ in range(rng.choice([0, 1, 1, 2])):
        parts.append(noise_kv(rng, pad, rng.choice(["env", "name", "if", "env2"])))
    rng.shuffle(parts)
    out += parts
    out.append(f"{pad}steps:\n")
    step_pad = pad + "  "

    def noise():
        k = rng.random()
        if k < 0.5:
            return f"{step_pad}- name: n\n" + noise_kv(rng, step_pad + "  ", rng.choice(["run", "env", "shell"]))
        if k < 0.8:
            return noise_kv(rng, step_pad, "- run")
        return f"{step_pad}- run: echo {junk(rng, 1, 3)}\n"

    steps = []
    for _ in range(rng.choice([0, 1, 1, 1, 2])):
        action = rng.choice(["actions/setup-python@v5"] * 3 + ["astral-sh/setup-uv@v6", "Actions/Setup-Python@v6"])
        inputs = []
        if rng.random() < 0.85:
            inputs.append(
                ("python-version", "${{ matrix.py }}" if use_matrix and rng.random() < 0.5 else rng.choice(versions))
            )
        if "ython" in action and rng.random() < 0.7:
            inputs.append(
                ("architecture", "${{ matrix.arch }}" if use_matrix and rng.random() < 0.5 else rng.choice(arches))
            )
        rng.shuffle(inputs)
        text = f"{step_pad}- uses: {quoted_or_plain(rng, action)}{atom_comment(rng)}\n"
        if inputs:
            lines = [f"{step_pad}    {k}: {quoted_or_plain(rng, v)}{atom_comment(rng)}\n" for k, v in inputs]
            if rng.random() < 0.3:
                lines.insert(rng.randint(0, len(lines)), noise_kv(rng, step_pad + "    ", "cache"))
            text += f"{step_pad}  with:\n" + "".join(lines)
        for _ in range(rng.choice([0, 0, 1])):
            steps.append(noise())
        steps.append(text)
    for _ in range(rng.choice([0, 1, 1, 2])):
        steps.append(noise())
    return "".join(out + steps)


# --- junk: junk values next to a fixed setup-python with architecture x86 on windows-latest (win32) ---

JUNK = [
    "[", "]", "{", "}", ", ", ",", ": ", ":", "'", '"', "'", '"', " #", " # ", "#", "- ", " - ", "? ", "!!str ", "&a ",
    "*a", "a", "b c", "x:", " ", "\\", "''", "|", ">", "NL", "NL", "x86", "windows-latest", "K: ", '"K":', "'K':",
    "\t", "\u00a0", "@", "%", "`", "=", "$", "${{ m }}", "}}",
]  # fmt: skip


def junk_text(rng, low, high, newline):
    return "".join(rng.choice(JUNK) for _ in range(rng.randint(low, high))).replace("NL", newline)


def junk_value(rng, pad):
    newline = "\n" + pad + "  " + " " * rng.randint(0, 2)
    kind = rng.randrange(6)
    if kind == 0:
        return "{" + junk_text(rng, 1, 8, newline) + "}"
    if kind == 1:
        return "[" + junk_text(rng, 1, 8, newline) + "]"
    if kind == 2:
        return junk_text(rng, 1, 8, newline)
    if kind == 3:
        return rng.choice("'\"") + junk_text(rng, 1, 6, newline) + rng.choice("'\"")
    if kind == 4:
        return "\n" + pad + "  " + rng.choice("[{") + junk_text(rng, 1, 6, newline) + rng.choice("]}")
    return (
        rng.choice(["{", "["])
        + junk_text(rng, 1, 4, newline)
        + newline
        + junk_text(rng, 0, 4, newline)
        + rng.choice("]}")
    )


def junk_workflow(rng):
    pad = "    "
    shape = rng.randrange(8)
    closer = junk_text(rng, 0, 3, " ")
    setup_noise = f"          cache: {junk_value(rng, '          ')}\n" if shape == 1 else ""
    pre = f"{pad}env: {junk_value(rng, pad)}\n" if shape in (0, 2, 3) else ""
    prestep = f"      - run: {junk_value(rng, '        ')}\n" if shape in (3, 4) else ""
    prestep2 = f"      - name: n\n        env: {junk_value(rng, '        ')}\n" if shape == 5 else ""
    matrix, runs_on = "", "windows-latest"
    if shape in (6, 7):
        runs_on = "${{ matrix.os }}"
        items = ["windows-latest", junk_text(rng, 1, 4, "\n            ")]
        rng.shuffle(items)
        if shape == 6:
            body = " [" + ", ".join(items) + "]"
        else:
            body = (
                "\n          [\n            " + ",\n            ".join(items) + rng.choice(["", ","]) + "\n          ]"
            )
        matrix = f"{pad}strategy:\n{pad}  matrix:\n{pad}    os:{body}\n"
    return (
        f"on: push\njobs:\n  b:\n{pad}runs-on: {runs_on}\n{matrix}{pre}{pad}steps:\n{prestep}{prestep2}"
        f"      - uses: actions/setup-python@v5\n        with:\n{setup_noise}"
        f"          python-version: '3.12'\n          architecture: x86\n      - run: echo {closer}\n"
    )


@pytest.mark.parametrize(
    ("generator", "cases", "least_valid", "least_exact"),
    [(realistic, 3000, 1500, 300), (atoms, 6000, 300, 50), (junk_workflow, 6000, 600, 200)],
)
def test_scan_never_disagrees_with_pyyaml(generator, cases, least_valid, least_exact):
    rng = random.Random(f"wheelgap-228-{generator.__name__}")
    valid = exact = 0
    for case in range(cases):
        text = generator(rng)
        try:
            expected_targets, expected_pythons = oracle(text)
        except (yaml.YAMLError, Skip):
            continue
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
        valid += 1
    print(f"{generator.__name__}: {valid} valid YAML, {exact} read in full")
    assert valid >= least_valid
    assert exact >= least_exact
