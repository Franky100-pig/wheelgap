from wheelgap.check import Status, check, marker_holds, status_for
from wheelgap.pypi import fill_all
from wheelgap.sources import Package, Source, load_inputs
from wheelgap.targets import build_targets


def results(rows):
    return {r.package.name: [s.value for s in r.results.values()] for r in rows}


def test_status_for():
    (lin,) = build_targets(["3.13"], ["linux-x86_64"])
    assert status_for(["a-1-cp313-cp313-manylinux_2_17_x86_64.whl"], lin) is Status.WHEEL
    assert status_for(["a-1-cp313-cp313-win_amd64.whl", "a-1.tar.gz"], lin) is Status.SOURCE
    assert status_for(["a-1-cp313-cp313-win_amd64.whl"], lin) is Status.MISSING
    assert status_for([], lin) is Status.MISSING
    assert status_for(None, lin) is Status.UNKNOWN
    assert status_for(["garbage.whl", "a-1.zip"], lin) is Status.SOURCE


def test_uv_lock_graph(fixtures):
    sources = load_inputs([str(fixtures / "uvproj")])
    targets = build_targets(["3.13"], ["linux-x86_64", "musl-aarch64", "win-amd64"])
    got = results(check(sources, targets))
    assert got["fastlib"] == ["wheel", "source", "wheel"]
    assert got["purewheel"] == ["wheel", "wheel", "wheel"]
    assert got["puresdist"] == ["source", "source", "source"]
    assert got["winonly"] == ["n/a", "n/a", "wheel"]  # only pulled in on Windows
    assert got["oldpy"] == ["n/a", "n/a", "n/a"]  # only on Python < 3.13
    assert got["devtool"] == ["wheel", "wheel", "wheel"]  # dev group is included
    assert got["orphan"] == ["n/a", "n/a", "n/a"]  # nothing depends on it
    assert got["nativeext"] == ["source", "source", "source"]
    assert "gitdep" not in got and "demo" not in got


def test_uv_lock_via(fixtures):
    sources = load_inputs([str(fixtures / "uvproj")])
    lin, musl, win = targets = build_targets(["3.13"], ["linux-x86_64", "musl-aarch64", "win-amd64"])
    rows = {r.package.name: r for r in check(sources, targets)}
    assert rows["nativeext"].via == {t: ["demo", "fastlib", "nativeext"] for t in targets}
    assert rows["winonly"].via == {win: ["demo", "winonly"]}  # n/a targets get no path
    assert rows["orphan"].via == {}


def graph(edges, root_edges=(), root_groups=None, roots=(0,)):
    """A uv.lock-style Source whose package names are the keys of `edges`, in order."""
    names = list(edges)
    packages = [Package(n, "1", files=[f"{n}-1.tar.gz"]) for n in names]
    for i in roots:
        packages[i].skip = "editable"
    deps = {names.index(n): [(d, None, m) for d, m in out] for n, out in edges.items()}
    return Source(
        "uv.lock", packages, edges=deps, roots=list(roots), root_edges=list(root_edges), root_groups=root_groups or {}
    )


def via(src, targets, name):
    return next(r for r in check([src], targets) if r.package.name == name).via


def test_via_is_a_shortest_path():
    # A depth-first walk pops `long` first and reaches `c` through long → x.
    src = graph(
        {
            "root": [("short", None), ("long", None)],
            "short": [("c", None)],
            "long": [("x", None)],
            "x": [("c", None)],
            "c": [],
        }
    )
    (t,) = build_targets(["3.13"], ["linux-x86_64"])
    assert via(src, [t], "c") == {t: ["root", "short", "c"]}


def test_via_differs_per_target():
    win_only = "sys_platform == 'win32'"
    src = graph(
        {"root": [("w", win_only), ("l", None)], "w": [("c", None)], "l": [("x", None)], "x": [("c", None)], "c": []}
    )
    lin, win = build_targets(["3.13"], ["linux-x86_64", "win-amd64"])
    assert via(src, [lin, win], "c") == {lin: ["root", "l", "x", "c"], win: ["root", "w", "c"]}


def test_via_from_dependency_group():
    src = graph(
        {"root": [], "tool": [("dep", None)], "dep": []}, root_edges=[("tool", None, None)], root_groups={0: "dev"}
    )
    (t,) = build_targets(["3.13"], ["linux-x86_64"])
    assert via(src, [t], "tool") == {t: ["(dev group)", "tool"]}
    assert via(src, [t], "dep") == {t: ["(dev group)", "tool", "dep"]}


def test_via_from_plain_requirement():
    src = graph({"req": []}, root_edges=[("req", None, None)], roots=())
    (t,) = build_targets(["3.13"], ["linux-x86_64"])
    assert via(src, [t], "req") == {t: ["req"]}


def test_uv_lock_graph_python_312(fixtures):
    sources = load_inputs([str(fixtures / "uvproj")])
    (t,) = build_targets(["3.12"], ["linux-x86_64"])
    got = results(check(sources, [t]))
    assert got["oldpy"] == ["source"]
    assert got["fastlib"] == ["source"]  # only cp313 wheels


def test_uv_version_disambiguation():
    src = Source(
        "uv.lock",
        packages=[
            Package("root", "0", skip="editable"),
            Package("np", "1.0", files=["np-1.0.tar.gz"]),
            Package("np", "2.0", files=["np-2.0-py3-none-any.whl"]),
        ],
        edges={0: [("np", "1.0", "python_version < '3.10'"), ("np", "2.0", "python_version >= '3.10'")], 1: [], 2: []},
        roots=[0],
    )
    (t,) = build_targets(["3.13"], ["linux-x86_64"])
    rows = check([src], [t])
    assert [(r.package.version, r.results[t].value) for r in rows] == [("1.0", "n/a"), ("2.0", "wheel")]


def test_markers_on_packages(fixtures):
    sources = load_inputs([str(fixtures / "pylockproj")])
    targets = build_targets(["3.13"], ["linux-aarch64", "macos-arm64"])
    got = results(check(sources, targets))
    assert got["fastlib"] == ["wheel", "missing"]
    assert got["macpkg"] == ["n/a", "source"]
    assert all(r.via == {} for r in check(sources, targets))  # no graph, no paths


def test_bad_marker_counts_as_needed():
    (t,) = build_targets(["3.13"], ["linux-x86_64"])
    assert marker_holds("this is not a marker", t)
    assert marker_holds(None, t)
    assert not marker_holds("sys_platform == 'win32'", t)


def test_requirements_against_fake_index(fixtures, fake_pypi):
    sources = load_inputs([str(fixtures / "reqproj")])
    packages = [p for s in sources for p in s.packages]
    fill_all(packages)
    targets = build_targets(["3.13"], ["linux-x86_64", "win-amd64"])
    got = results(check(sources, targets))
    assert got == {
        "fastlib": ["wheel", "source"],
        "unpinned": ["wheel", "wheel"],
        "winonly": ["n/a", "wheel"],
        "purewheel": ["wheel", "wheel"],
    }
    assert next(p for p in packages if p.name == "unpinned").version == "4.2"
    assert not any("localpkg" in c for c in fake_pypi)


def test_lookup_errors_are_unknown(fake_pypi):
    pkgs = [Package("doesnotexist", "1.0"), Package("fastlib", "9.9")]
    fill_all(pkgs)
    assert [p.error for p in pkgs] == ["not on index", "not on index"]
    (t,) = build_targets(["3.13"], ["linux-x86_64"])
    rows = check([Source("x", pkgs)], [t])
    assert [r.results[t] for r in rows] == [Status.UNKNOWN, Status.UNKNOWN]


def test_online_refresh_overrides_lockfile(fixtures, fake_pypi):
    sources = load_inputs([str(fixtures / "uvproj")])
    packages = [p for s in sources for p in s.packages]
    fill_all(packages, refresh=True)
    fastlib = next(p for p in packages if p.name == "fastlib")
    assert fastlib.files == ["fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl", "fastlib-2.0.0.tar.gz"]


def test_pyodide_sdist_only_is_missing():
    (py,) = build_targets(["3.13"], ["pyodide"])
    (lin,) = build_targets(["3.13"], ["linux-x86_64"])
    assert status_for(["a-1.tar.gz"], lin) is Status.SOURCE
    assert status_for(["a-1.tar.gz"], py) is Status.MISSING  # no compiler in the browser
    assert status_for(["a-1-cp313-cp313-pyodide_2025_0_wasm32.whl", "a-1.tar.gz"], py) is Status.WHEEL


def test_via_prefers_the_projects_own_dependency_over_a_group():
    # numpy is a runtime dependency of the project and also listed in the dev group.
    src = graph({"app": [("numpy", None)], "numpy": []}, root_edges=[("numpy", None, None)], root_groups={0: "dev"})
    (t,) = build_targets(["3.13"], ["linux-x86_64"])
    assert via(src, [t], "numpy") == {t: ["app", "numpy"]}


def test_via_prefers_a_plain_requirement_over_a_group():
    src = graph(
        {"a": [("c", None)], "b": [("c", None)], "c": []},
        root_edges=[("a", None, None), ("b", None, None)],
        root_groups={0: "dev"},
        roots=(),
    )
    (t,) = build_targets(["3.13"], ["linux-x86_64"])
    assert via(src, [t], "c") == {t: ["b", "c"]}
