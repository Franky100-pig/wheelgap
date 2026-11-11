import io
import json

import pytest

from wheelgap import __version__, mcp


def run(*messages: object, raw: bytes = b"") -> list:
    """Send messages to a fresh server over in-memory stdio; return the parsed replies, one per output line."""
    stdin = io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in messages) + raw)
    stdout = io.BytesIO()
    mcp.serve(stdin, stdout)
    lines = stdout.getvalue().splitlines()
    return [json.loads(line) for line in lines]


def init(version: str = "2025-06-18") -> dict:
    return {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": version}}


def call(tool: str, msg_id: int = 1, **arguments: object) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}


@pytest.mark.parametrize(
    ("asked", "agreed"),
    [("2024-11-05", "2024-11-05"), ("2025-11-25", "2025-11-25"), ("2099-01-01", "2025-11-25"), (None, "2025-11-25")],
)
def test_initialize_negotiates_version(asked, agreed):
    (reply,) = run(init(asked))
    result = reply["result"]
    assert reply["id"] == 0
    assert result["protocolVersion"] == agreed
    assert result["serverInfo"] == {"name": "wheelgap", "version": __version__}
    assert result["capabilities"] == {"tools": {"listChanged": False}}


def test_notifications_get_no_reply_and_unknown_methods_are_errors():
    replies = run(
        init(),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 9}},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        # Clients on the 2026 revisions probe with server/discover and fall back to initialize on any error.
        {"jsonrpc": "2.0", "id": 3, "method": "server/discover", "params": {}},
    )
    assert [r["id"] for r in replies] == [0, 2, 3]
    assert replies[1]["result"] == {}
    assert replies[2]["error"]["code"] == mcp.METHOD_NOT_FOUND


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"{not json\n", mcp.PARSE_ERROR),
        (b"\xff\xfe\n", mcp.PARSE_ERROR),
        (b'{"id": 1, "method": "ping"}\n', mcp.INVALID_REQUEST),
        (b'{"jsonrpc": "2.0", "id": true, "method": "ping"}\n', mcp.INVALID_REQUEST),
        (b'{"jsonrpc": "2.0", "id": 1, "method": "ping", "params": []}\n', mcp.INVALID_PARAMS),
        (b"[]\n", mcp.INVALID_REQUEST),
        (b'{"jsonrpc": "2.0", "id": ' + b"9" * 5000 + b', "method": "ping"}\n', mcp.PARSE_ERROR),
        (b"[" * 100_000 + b"\n", mcp.PARSE_ERROR),
        (b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": ["x"]}}\n', mcp.INVALID_PARAMS),
    ],
)
def test_bad_messages_get_errors_and_the_server_keeps_going(raw, code):
    replies = run(raw=raw + b'\n{"jsonrpc": "2.0", "id": 7, "method": "ping"}\r\n')
    assert replies[0]["error"]["code"] == code
    assert replies[-1] == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_responses_from_the_client_get_no_reply():
    replies = run({"jsonrpc": "2.0", "id": 3, "result": {}}, {"jsonrpc": "2.0", "id": 4, "method": "ping"})
    assert [r["id"] for r in replies] == [4]


def test_utf8_bom_on_the_first_line():
    (reply,) = run(raw=b"\xef\xbb\xbf" + json.dumps(init()).encode() + b"\n")
    assert reply["result"]["serverInfo"]["name"] == "wheelgap"


def test_batches_reply_with_a_list():
    (reply,) = run([{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "method": "x"}])
    assert reply == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


def test_tools_list_has_valid_schemas():
    (_, reply) = run(init(), {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in reply["result"]["tools"]}
    assert set(tools) == {"check_package", "check_lockfile", "list_targets"}
    for tool in tools.values():
        assert tool["inputSchema"]["type"] == "object"
        assert tool["annotations"]["readOnlyHint"] is True
        assert set(tool["inputSchema"].get("required", [])) <= set(tool["inputSchema"]["properties"])


def test_check_package(fake_pypi):
    (_, reply) = run(init(), call("check_package", name="winonly", python="3.13", targets=["win-amd64", "linux/arm64"]))
    result = reply["result"]
    data = json.loads(result["content"][0]["text"])
    assert result["isError"] is False
    assert result["structuredContent"] == data
    assert data["results"] == {"3.13 win-amd64": "wheel", "3.13 linux-aarch64": "missing"}
    assert data["ok"] is False
    assert data["gaps"] == ["3.13 linux-aarch64"]
    assert data["version"] == "311"
    assert data["assumes"] == {"glibc": "2.36", "musl": "1.2", "macos": "14.0"}
    assert fake_pypi == ["https://pypi.org/pypi/winonly/json"]


def test_check_package_pinned_version_and_glibc(fake_pypi):
    (_, reply) = run(
        init(),
        call("check_package", name="fastlib", version="2.0.0", python="3.13", targets=["linux-x86_64"], glibc="2.17"),
    )
    data = reply["result"]["structuredContent"]
    assert fake_pypi == ["https://pypi.org/pypi/fastlib/2.0.0/json"]
    assert data["results"] == {"3.13 linux-x86_64": "wheel"}  # manylinux_2_17 still fits glibc 2.17
    assert data["assumes"]["glibc"] == "2.17"


def test_structured_content_only_from_2025_06_18(fake_pypi):
    (_, reply) = run(init("2025-03-26"), call("check_package", name="purewheel", python="3.13"))
    assert "structuredContent" not in reply["result"]
    assert json.loads(reply["result"]["content"][0]["text"])["ok"] is True


def test_check_package_lookup_failure_is_unknown_not_ok(fake_pypi):
    (_, reply) = run(init(), call("check_package", name="no-such-package", python="3.13", targets=["linux-x86_64"]))
    data = reply["result"]["structuredContent"]
    assert data["ok"] is False
    assert data["results"] == {"3.13 linux-x86_64": "unknown"}
    assert data["error"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "'name' is required"),
        ({"name": ""}, "'name' must be a non-empty string"),
        ({"name": "numpy", "targets": "linux-aarch64"}, "'targets' must be an array of strings"),
        ({"name": "numpy", "targets": ["linux-sparc"]}, "linux-sparc"),
        ({"name": "numpy", "python": "three"}, "three"),
        ({"name": "numpy", "platforms": ["win32"]}, "unknown argument(s): platforms"),
        ({"name": "numpy==2.0"}, "isn't a package name"),
        ({"name": "numpy", "version": ">=2"}, "exact version"),
        ({"name": "numpy", "glibc": "new"}, "glibc"),
    ],
)
def test_bad_arguments_are_tool_errors_without_lookups(fake_pypi, arguments, message):
    (_, reply) = run(init(), call("check_package", **arguments))
    assert reply["result"]["isError"] is True
    assert message in reply["result"]["content"][0]["text"]
    assert not fake_pypi


def test_unknown_tool_is_a_protocol_error():
    (_, reply) = run(init(), call("rm_rf"))
    assert reply["error"]["code"] == mcp.INVALID_PARAMS


def test_check_lockfile_reports_only_gaps(fake_pypi, fixtures):
    (_, reply) = run(
        init(), call("check_lockfile", path=str(fixtures / "uvproj"), python="3.13", targets=["linux-x86_64"])
    )
    data = reply["result"]["structuredContent"]
    assert data["targets"] == ["3.13 linux-x86_64"]
    assert data["ok"] is False
    assert data["packages"] == 5
    assert {g["name"]: g["results"] for g in data["gaps"]} == {
        "nativeext": {"3.13 linux-x86_64": "source"},
        "puresdist": {"3.13 linux-x86_64": "source"},
    }


def test_check_lockfile_lists_skipped_packages(fake_pypi, fixtures):
    (_, reply) = run(
        init(), call("check_lockfile", path=str(fixtures / "uvproj"), python="3.13", targets=["linux-x86_64"])
    )
    data = reply["result"]["structuredContent"]
    assert {s["name"]: s["reason"] for s in data["skipped"]} == {"demo": "editable", "gitdep": "git"}


def test_check_lockfile_uses_project_config_and_arguments_win(fake_pypi, fixtures):
    path = str(fixtures / "configproj")
    (_, configured, explicit) = run(
        init(),
        call("check_lockfile", path=path),
        call("check_lockfile", 2, path=path, python="3.13", targets=["linux-x86_64"]),
    )
    assert configured["result"]["structuredContent"]["targets"] == ["3.13 win-amd64", "3.14t win-amd64"]
    assert explicit["result"]["structuredContent"]["targets"] == ["3.13 linux-x86_64"]


def test_check_lockfile_offline_config_means_no_lookups(fake_pypi, tmp_path):
    (tmp_path / "requirements.txt").write_text("purewheel==3.1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\noffline = true\n", encoding="utf-8")
    (_, reply) = run(init(), call("check_lockfile", path=str(tmp_path / "requirements.txt"), python="3.13"))
    assert reply["result"]["isError"] is True
    assert "offline" in reply["result"]["content"][0]["text"]
    assert not fake_pypi


def test_check_lockfile_ignores_from_config(fake_pypi, tmp_path, fixtures):
    (tmp_path / "uv.lock").write_bytes((fixtures / "uvproj" / "uv.lock").read_bytes())
    (tmp_path / "pyproject.toml").write_text(
        '[tool.wheelgap]\nignore = ["nativeext:linux-x86_64", "puresdist"]\n', encoding="utf-8"
    )
    (_, reply) = run(
        init(),
        call("check_lockfile", path=str(tmp_path), python="3.13", targets=["linux-x86_64", "win-amd64"], offline=True),
    )
    data = reply["result"]["structuredContent"]
    assert data["ignored"] == ["puresdist"]
    # nativeext's linux gap is accepted; its Windows gap still counts.
    assert [(g["name"], g["gaps"]) for g in data["gaps"]] == [("nativeext", ["3.13 win-amd64"])]


def test_check_lockfile_glibc_argument(fake_pypi, fixtures):
    path = str(fixtures / "uvproj")
    (_, default, old) = run(
        init(),
        call("check_lockfile", path=path, python="3.13", targets=["linux-x86_64"], offline=True),
        call("check_lockfile", 2, path=path, python="3.13", targets=["linux-x86_64"], offline=True, glibc="2.12"),
    )
    assert "fastlib" not in {g["name"] for g in default["result"]["structuredContent"]["gaps"]}
    # fastlib's only wheel is manylinux_2_17, too new for glibc 2.12.
    assert "fastlib" in {g["name"] for g in old["result"]["structuredContent"]["gaps"]}


def test_check_lockfile_counts_unknown(fake_pypi, tmp_path):
    (tmp_path / "requirements.txt").write_text("purewheel==3.1\nno-such-package==1.0\n", encoding="utf-8")
    (_, reply) = run(init(), call("check_lockfile", path=str(tmp_path / "requirements.txt"), python="3.13"))
    data = reply["result"]["structuredContent"]
    assert data["unknown"] == 1 and data["ok"] is False


def test_check_lockfile_path_starting_with_a_dash(fake_pypi, tmp_path, monkeypatch, fixtures):
    (tmp_path / "-p").mkdir()
    (tmp_path / "-p" / "uv.lock").write_bytes((fixtures / "uvproj" / "uv.lock").read_bytes())
    monkeypatch.chdir(tmp_path)
    (_, reply) = run(init(), call("check_lockfile", path="-p", python="3.13", offline=True))
    assert reply["result"]["isError"] is False
    assert reply["result"]["structuredContent"]["path"].endswith("-p")


def test_check_lockfile_argparse_exit_is_a_tool_error(monkeypatch, fixtures):
    from wheelgap import cli

    def exits(argv):
        raise SystemExit(2)

    monkeypatch.setattr(cli, "main", exits)
    (_, reply, ping) = run(
        init(), call("check_lockfile", path=str(fixtures / "uvproj")), {"jsonrpc": "2.0", "id": 9, "method": "ping"}
    )
    assert reply["result"]["isError"] is True
    assert ping["result"] == {}


def test_check_lockfile_passes_on_notes(fake_pypi, fixtures):
    (_, reply) = run(
        init(),
        call("check_lockfile", path=str(fixtures / "uvproj"), python="3.11", targets=["linux-x86_64"], offline=True),
    )
    notes = reply["result"]["structuredContent"]["notes"]
    assert "may show false gaps" in notes[0]
    assert notes[1] == "2 packages weren't checked (see skipped); ok covers the others only"
    assert not fake_pypi


def test_check_lockfile_explicit_python_beats_python_version_file(fake_pypi, tmp_path, fixtures):
    (tmp_path / "uv.lock").write_bytes((fixtures / "uvproj" / "uv.lock").read_bytes())
    (tmp_path / ".python-version").write_text("3.12\n", encoding="utf-8")
    (_, reply) = run(init(), call("check_lockfile", path=str(tmp_path), python="3.13", offline=True))
    assert {t.split()[0] for t in reply["result"]["structuredContent"]["targets"]} == {"3.13"}


def test_check_lockfile_uses_python_version_file(fake_pypi, tmp_path, fixtures):
    (tmp_path / "uv.lock").write_bytes((fixtures / "uvproj" / "uv.lock").read_bytes())
    (tmp_path / ".python-version").write_text("3.12.4\n", encoding="utf-8")
    (_, reply) = run(init(), call("check_lockfile", path=str(tmp_path), targets=["linux-x86_64"], offline=True))
    assert reply["result"]["structuredContent"]["targets"] == ["3.12 linux-x86_64"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"path": "/no/such/uv.lock"}, "no such file or directory"),
        ({"path": "."}, None),
        ({"path": "x", "offline": "yes"}, "'offline' must be true or false"),
    ],
)
def test_check_lockfile_errors(fake_pypi, tmp_path, monkeypatch, arguments, message):
    monkeypatch.chdir(tmp_path)  # an empty directory: no lockfile to find
    (tmp_path / "x").write_text("", encoding="utf-8")
    (_, reply) = run(init(), call("check_lockfile", **arguments))
    assert reply["result"]["isError"] is True
    if message:
        assert message in reply["result"]["content"][0]["text"]


def test_list_targets():
    (_, reply) = run(init(), call("list_targets"))
    data = reply["result"]["structuredContent"]
    assert "musl-aarch64" in data["platforms"]
    assert data["groups"]["all"] and set(data["groups"]["all"]) <= set(data["platforms"])
    assert set(data["aliases"].values()) <= set(data["platforms"])


def test_stray_prints_stay_off_the_protocol_stream(monkeypatch):
    def noisy(args):
        print("progress...")
        return {"done": True}

    monkeypatch.setitem(mcp._HANDLERS, "list_targets", noisy)
    (_, reply) = run(init(), call("list_targets"))
    assert reply["result"]["structuredContent"] == {"done": True}


def test_crash_in_a_tool_is_reported_not_fatal(monkeypatch):
    def broken(args):
        raise KeyError("boom")

    monkeypatch.setitem(mcp._HANDLERS, "list_targets", broken)
    (_, reply, ping) = run(init(), call("list_targets"), {"jsonrpc": "2.0", "id": 5, "method": "ping"})
    assert reply["result"]["isError"] is True
    assert "KeyError" in reply["result"]["content"][0]["text"]
    assert ping["result"] == {}


def test_non_ascii_output_stays_on_one_line(monkeypatch):
    monkeypatch.setitem(mcp._HANDLERS, "list_targets", lambda args: {"text": "naïve\nline"})
    stdin = io.BytesIO(json.dumps(call("list_targets")).encode() + b"\n")
    stdout = io.BytesIO()
    mcp.serve(stdin, stdout)
    assert stdout.getvalue().count(b"\n") == 1
    assert json.loads(stdout.getvalue())["result"]["structuredContent"]["text"] == "naïve\nline"


def test_real_stdio(fixtures):
    import subprocess
    import sys

    messages = [init(), {"jsonrpc": "2.0", "method": "notifications/initialized"}, call("list_targets")]
    proc = subprocess.run(
        [sys.executable, "-m", "wheelgap.mcp"],
        input=b"".join(json.dumps(m).encode() + b"\r\n" for m in messages),
        capture_output=True,
        timeout=60,
        check=True,
    )
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [r["id"] for r in replies] == [0, 1]
    assert "linux-aarch64" in replies[1]["result"]["structuredContent"]["platforms"]


@pytest.mark.parametrize(("argv", "code"), [(["--version"], 0), (["--help"], 0), (["--bogus"], 2)])
def test_main_flags(argv, code, capsys):
    assert mcp.main(argv) == code
