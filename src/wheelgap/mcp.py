"""An MCP server, so coding agents can ask wheelgap whether a package has a wheel for a platform.

Run it with `python -m wheelgap.mcp` (or the `wheelgap-mcp` command). It speaks the Model Context Protocol over
stdio: one JSON-RPC 2.0 message per line on stdin and stdout, using the initialize handshake of the 2024-11-05 to
2025-11-25 protocol revisions. Newer clients that first probe with server/discover get "method not found" and fall
back to that handshake. Like the rest of wheelgap it needs only the standard library and packaging.

Tools (all read-only; they look packages up on PyPI, or the index in WHEELGAP_INDEX_URL):
- check_package: one package (latest release or a given version) on each target.
- check_lockfile: what `wheelgap PATH --json` reports, so project config, notes and skipped packages match the CLI.
- list_targets: the platform names the other tools accept.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

from packaging.version import InvalidVersion, Version

from . import __version__
from .api import Result, check_package, make_targets, running_python
from .check import Status
from .pyodide import PyodideLockError, load_pyodide_lock
from .sources import SourceError
from .targets import (
    ALIASES,
    DEFAULT_GLIBC,
    DEFAULT_MACOS,
    DEFAULT_MUSL,
    DEFAULT_PLATFORMS,
    EVERY_NATIVE_PLATFORM,
    MOBILE_PLATFORMS,
    NATIVE_PLATFORMS,
    PLATFORMS,
    TargetError,
)

# The handshake-era revisions, oldest first. A client asking for one of these gets it back; any other version gets the
# newest, and the client decides whether it can use that (as the MCP lifecycle spec says).
PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
_STRUCTURED_FROM = "2025-06-18"  # the first revision with structuredContent in tool results

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

INSTRUCTIONS = (
    "wheelgap tells you whether pip or uv can install a package from a prebuilt wheel on a platform, or will have to "
    "build it from source (or can't install it at all). Use check_lockfile on a project's uv.lock, poetry.lock, "
    "pdm.lock, pylock.toml, Pipfile.lock or requirements.txt before changing the platforms it deploys to, and "
    "check_package before adding a dependency. Pass the project's Python version: results differ between versions. "
    "Linux results assume a glibc (manylinux) or musl version: pass the one your image or distro has."
)

_STATUS_HELP = (
    "Statuses: 'wheel' (a compatible wheel exists), 'source' (no wheel, so installers build the sdist and need a "
    "compiler and headers), 'missing' (nothing installable: no wheel and no sdist, or, on pyodide, iOS and Android, "
    "which can't build sdists, no wheel), 'unknown' (the lookup failed: not a pass), 'n/a' (environment markers say "
    "it isn't installed there), 'bundled' (Pyodide ships its own build). On pyodide, packages Pyodide ships itself "
    "(numpy and many others) show 'source' or 'missing' unless a pyodide_lock is given (check_lockfile also reads "
    "[tool.wheelgap] pyodide-lock)."
)

_PYTHON_PROP = {
    "type": "string",
    "description": "Target Python version(s), comma-separated, e.g. '3.13' or '3.12,3.14t' (t = free-threaded).",
}
_TARGETS_PROP = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Platforms, e.g. ['linux-aarch64', 'musl-x86_64', 'win-arm64', 'pyodide'], Docker platforms such as "
        f"'linux/arm64', or the groups 'all', 'everything' and 'mobile'. Default: {', '.join(DEFAULT_PLATFORMS)}. "
        "list_targets gives every name."
    ),
}
_VERSION_PROPS = {
    "glibc": {
        "type": "string",
        "description": f"glibc version for linux-* targets, e.g. '2.28' for a RHEL 8 image. Default: {DEFAULT_GLIBC}.",
    },
    "musl": {"type": "string", "description": f"musl version for musl-* (Alpine) targets. Default: {DEFAULT_MUSL}."},
    "macos": {"type": "string", "description": f"Oldest macOS version to support. Default: {DEFAULT_MACOS}."},
}
_PYODIDE_LOCK_PROP = {
    "type": "string",
    "description": (
        "Absolute path or https URL of a Pyodide release's pyodide-lock.json, so packages that release ships itself "
        "count as 'bundled' on pyodide targets for its Python. Its Python must be one a pyodide target uses. "
        "Relative paths are resolved against the server's working directory."
    ),
}
_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "check_package",
        "title": "Check a package's wheels",
        "description": (
            "Check whether one PyPI package has a prebuilt wheel for each target platform and Python. Returns each "
            "target's status, the targets with a gap, ok (true only if every target installs without building) and "
            "the libc and macOS versions assumed. " + _STATUS_HELP
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The package name alone, e.g. 'numpy'."},
                "version": {
                    "type": "string",
                    "description": "An exact version, e.g. '2.3.4'. Default: the latest release, which may not "
                    "support an older Python (it then shows gaps an older release wouldn't have).",
                },
                "python": _PYTHON_PROP | {"description": _PYTHON_PROP["description"] + " Default: the server's."},
                "targets": _TARGETS_PROP,
                **_VERSION_PROPS,
                "pyodide_lock": _PYODIDE_LOCK_PROP,
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "check_lockfile",
        "title": "Check a lockfile's wheels",
        "description": (
            "Check every package a lockfile installs (uv.lock, poetry.lock, pdm.lock, pylock.toml, Pipfile.lock, "
            "requirements.txt, or a project directory containing one), exactly as the wheelgap command line does, and "
            "report the packages without a wheel on some target. Environment markers, .python-version and the "
            "project's [tool.wheelgap] settings (targets, python, glibc, ignore, offline, index-url, ...) apply; "
            "arguments given here override them, except that offline = true in the project can't be turned off. "
            "Notes and errors name command-line flags (--offline is the offline argument here). "
            "Packages from git, paths, URLs or private indexes are listed under "
            "'skipped': they were NOT checked. 'notes' says when an answer may be incomplete. Requirements files are "
            "looked up on the index. " + _STATUS_HELP
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to the lockfile or project directory (relative paths are resolved "
                    "against the server's working directory).",
                },
                "python": _PYTHON_PROP
                | {
                    "description": _PYTHON_PROP["description"] + " Default: [tool.wheelgap] python, then "
                    ".python-version next to the lockfile, else the server's."
                },
                "targets": _TARGETS_PROP,
                **_VERSION_PROPS,
                "pyodide_lock": _PYODIDE_LOCK_PROP
                | {"description": _PYODIDE_LOCK_PROP["description"] + " Default: [tool.wheelgap] pyodide-lock."},
                "offline": {
                    "type": "boolean",
                    "description": "Use only the files recorded in the lockfile; never contact the index.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "list_targets",
        "title": "List target platforms",
        "description": "List the platform names and groups that check_package and check_lockfile accept.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "annotations": _READ_ONLY | {"openWorldHint": False},
    },
]


class ToolError(Exception):
    """A problem with the tool's arguments or input, reported to the model as a tool result with isError."""


_PYTHON_RE = re.compile(r"\d+\.\d+t?", re.ASCII)
_NAME_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?", re.ASCII)
_GAP = {Status.SOURCE.value, Status.MISSING.value, Status.UNKNOWN.value}


def _check_keys(tool: str, args: dict[str, Any]) -> None:
    """Models guess argument names ('platforms', 'python_version'): refuse them rather than ignore them."""
    known = next(t for t in TOOLS if t["name"] == tool)["inputSchema"]["properties"]
    if unknown := sorted(set(args) - set(known)):
        raise ToolError(f"unknown argument(s): {', '.join(unknown)} (accepted: {', '.join(known) or 'none'})")


def _string(args: dict[str, Any], key: str, *, required: bool = False) -> str | None:
    value = args.get(key)
    if value is None:
        if required:
            raise ToolError(f"'{key}' is required")
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise ToolError(f"'{key}' must be a non-empty string" if required else f"'{key}' must be a string")
    return value


def _platforms(args: dict[str, Any]) -> list[str]:
    platforms = args.get("targets")
    if platforms is None:
        return []
    if not isinstance(platforms, list) or not all(isinstance(p, str) for p in platforms):
        raise ToolError("'targets' must be an array of strings")
    return [p.strip() for p in platforms if p.strip()]


def _pythons(args: dict[str, Any]) -> list[str]:
    python = _string(args, "python") or ""
    parts = [p.strip() for p in python.split(",") if p.strip()]
    if bad := [p for p in parts if not _PYTHON_RE.fullmatch(p)]:
        raise ToolError(f"can't read Python version(s) {', '.join(map(repr, bad))} (expected e.g. '3.13' or '3.14t')")
    return parts


def _versions(args: dict[str, Any]) -> dict[str, str]:
    out = {}
    for key in _VERSION_PROPS:
        value = _string(args, key)
        if value is not None and value.strip():
            out[key] = value.strip()
    return out


def _lock_spec(args: dict[str, Any]) -> str:
    spec = (_string(args, "pyodide_lock") or "").strip()
    return spec if not spec or "://" in spec else str(Path(spec).expanduser())


def _check_package(args: dict[str, Any]) -> dict[str, Any]:
    _check_keys("check_package", args)
    name = (_string(args, "name", required=True) or "").strip()
    if not _NAME_RE.fullmatch(name):
        raise ToolError(f"{name!r} isn't a package name: pass the name alone, and an exact version as 'version'")
    version = (_string(args, "version") or "").strip() or None
    if version is not None:
        try:
            Version(version)
        except InvalidVersion:
            raise ToolError(f"'version' must be an exact version such as '2.3.4', not {version!r}") from None
    versions = _versions(args)
    try:
        targets = make_targets(_pythons(args) or None, _platforms(args) or None, **versions)
    except TargetError as e:
        raise ToolError(str(e)) from e
    lock_spec = _lock_spec(args)
    try:
        locks = [load_pyodide_lock(lock_spec)] if lock_spec else []
    except ValueError as e:  # PyodideLockError, or a file that isn't UTF-8
        raise ToolError(f"{lock_spec}: {e}" if isinstance(e, UnicodeDecodeError) else str(e)) from e
    except OSError as e:  # a lock path that can't be read
        raise ToolError(f"{lock_spec}: {e.strerror or e}") from e
    try:
        result: Result = check_package(name, version, targets, pyodide_locks=locks)
    except PyodideLockError as e:  # a lock for a Python no pyodide target uses
        raise ToolError(str(e)) from e
    out: dict[str, Any] = result.to_dict()
    out["ok"] = result.ok
    out["gaps"] = [t.label(True) for t in result.gaps]
    out["targets"] = [t.label(True) for t in targets]
    out["assumes"] = {
        "glibc": versions.get("glibc", DEFAULT_GLIBC),
        "musl": versions.get("musl", DEFAULT_MUSL),
        "macos": versions.get("macos", DEFAULT_MACOS),
    }
    return out


def _check_lockfile(args: dict[str, Any]) -> dict[str, Any]:
    """Run the command line itself with --json, so config, notes and skipped packages are exactly the CLI's."""
    from . import cli

    _check_keys("check_lockfile", args)
    raw = _string(args, "path", required=True) or ""
    offline = args.get("offline")
    if offline is not None and not isinstance(offline, bool):
        raise ToolError("'offline' must be true or false")
    path = Path(raw).expanduser()
    if not path.exists():
        raise ToolError(f"{raw}: no such file or directory (pass an absolute path)")
    # --name=value forms, and -- before the path, so no value can be read as another option.
    argv = ["--json"]
    argv += [f"--python={p}" for p in _pythons(args)]
    argv += [f"--target={p}" for p in _platforms(args)]
    argv += [f"--{key}={value}" for key, value in _versions(args).items()]
    if lock_spec := _lock_spec(args):
        argv.append(f"--pyodide-lock={lock_spec}")
    if offline:
        argv.append("--offline")
    argv += ["--", str(path)]
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
    except SystemExit as e:  # argparse rejected a value
        code = e.code if isinstance(e.code, int) else 2
    messages = [line.removeprefix("wheelgap: ") for line in err.getvalue().splitlines() if line.strip()]
    notes = [m.removeprefix("note: ") for m in messages if m.startswith("note: ")]
    # SystemExit is a BaseException: without the handler above it would end the server, not just this call.
    if code not in (0, 1):
        errors = [m.removeprefix("error: ") for m in messages if not m.startswith("note: ")]
        raise ToolError("; ".join(errors) or f"wheelgap exited with status {code}")
    data = json.loads(out.getvalue())
    # The CLI leaves the Python out of labels when there's only one; always give it, as check_package does.
    label = {t["label"]: f"{t['python']} {t['platform']}" for t in data["targets"]}
    platform_of = {t["label"]: t["platform"] for t in data["targets"]}
    ignored_on = {i["name"]: set(i["platforms"]) for i in data["ignored_targets"]}
    gaps = []
    for package in data["packages"]:
        cells = [
            label[cell]
            for cell, status in package["results"].items()
            if status in _GAP and platform_of[cell] not in ignored_on.get(package["name"], ())
        ]
        if cells:
            results = {label[cell]: status for cell, status in package["results"].items()}
            gaps.append(package | {"results": results, "gaps": cells})
    # A bundled build at another version still passes, as in the CLI, but a pin micropip can't install must be said.
    differs = [p for p in data["packages"] if p.get("bundled_version_differs")]
    for p in differs:
        notes.append(
            f"{p['name']}: Pyodide ships {p['bundled']}, not the {p['version']} asked for; it counts as bundled, "
            "but micropip may not install that version"
        )
    if data["skipped"]:
        n = len(data["skipped"])
        notes.append(f"{n} package{'s' if n != 1 else ''} weren't checked (see skipped); ok covers the others only")
    return {
        "path": str(path.resolve()),
        "targets": list(label.values()),
        "packages": data["summary"]["packages"],
        "ok": not gaps,
        "unknown": sum(1 for g in gaps if Status.UNKNOWN.value in g["results"].values()),
        "bundled_version_differs": [
            {"name": p["name"], "version": p["version"], "bundled": p["bundled"]} for p in differs
        ],
        "gaps": gaps,
        "skipped": data["skipped"],
        "ignored": data["ignored"],
        "ignored_targets": data["ignored_targets"],
        "notes": notes,
    }


def _list_targets(args: dict[str, Any]) -> dict[str, Any]:
    _check_keys("list_targets", args)
    return {
        "platforms": list(PLATFORMS),
        "default": list(DEFAULT_PLATFORMS),
        "groups": {"all": NATIVE_PLATFORMS, "everything": EVERY_NATIVE_PLATFORM, "mobile": MOBILE_PLATFORMS},
        "aliases": dict(ALIASES),
        "defaults": {"glibc": DEFAULT_GLIBC, "musl": DEFAULT_MUSL, "macos": DEFAULT_MACOS},
        "server_python": running_python(),
    }


_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "check_package": _check_package,
    "check_lockfile": _check_lockfile,
    "list_targets": _list_targets,
}


class Server:
    """The protocol state for one stdio connection."""

    def __init__(self) -> None:
        self.protocol_version = PROTOCOL_VERSIONS[-1]

    def handle(self, message: object) -> dict[str, Any] | list[dict[str, Any]] | None:
        """The response to one parsed message (or batch), or None for notifications."""
        if isinstance(message, list):  # JSON-RPC batches (only 2025-03-26 allowed them, but they cost nothing)
            if not message:
                return _error(None, INVALID_REQUEST, "empty batch")
            replies = [r for r in (self._one(m) for m in message) if r is not None]
            return replies or None
        return self._one(message)

    def _one(self, message: object) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(_id(message), INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        method = message.get("method")
        if "id" not in message or ("method" not in message and ("result" in message or "error" in message)):
            return None  # a notification (initialized, cancelled, ...) or a response to us: nothing to send
        msg_id = message["id"]
        if not isinstance(msg_id, (str, int)) or isinstance(msg_id, bool):
            return _error(None, INVALID_REQUEST, "id must be a string or an integer")
        if not isinstance(method, str):
            return _error(msg_id, INVALID_REQUEST, "method must be a string")
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _error(msg_id, INVALID_PARAMS, "params must be an object")
        if method == "initialize":
            return _reply(msg_id, self._initialize(params))
        if method == "ping":
            return _reply(msg_id, {})
        if method == "tools/list":
            return _reply(msg_id, {"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or name not in _HANDLERS:
                return _error(msg_id, INVALID_PARAMS, f"unknown tool: {name!r}")
            arguments = params.get("arguments")
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                return _error(msg_id, INVALID_PARAMS, "arguments must be an object")
            return _reply(msg_id, self._call(name, arguments))
        return _error(msg_id, METHOD_NOT_FOUND, f"method not found: {method}")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        asked = params.get("protocolVersion")
        self.protocol_version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[-1]
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "wheelgap", "version": __version__},
            "instructions": INSTRUCTIONS,
        }

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            # wheelgap's own messages go to stderr already; this keeps any stray print off the protocol stream.
            with contextlib.redirect_stdout(sys.stderr):
                data = _HANDLERS[name](arguments)
        except (ToolError, TargetError, SourceError) as e:
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        except Exception as e:  # a bug: report it to the model rather than end the connection
            return {"content": [{"type": "text", "text": f"internal error: {type(e).__name__}: {e}"}], "isError": True}
        result: dict[str, Any] = {"content": [{"type": "text", "text": json.dumps(data, indent=1)}], "isError": False}
        if self.protocol_version >= _STRUCTURED_FROM:
            result["structuredContent"] = data
        return result


def _id(message: object) -> Any:
    msg_id = message.get("id") if isinstance(message, dict) else None
    return msg_id if isinstance(msg_id, (str, int)) and not isinstance(msg_id, bool) else None


def _reply(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def serve(stdin: BinaryIO, stdout: BinaryIO) -> None:
    """Answer messages from stdin until it closes."""
    server = Server()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line.decode("utf-8-sig"))
        except (ValueError, RecursionError) as e:  # bad UTF-8 or JSON, an over-long integer, absurd nesting
            reply: object = _error(None, PARSE_ERROR, f"parse error: {type(e).__name__}")
        else:
            try:
                reply = server.handle(message)
            except Exception as e:  # never let one bad message end the connection
                reply = _error(_id(message), INTERNAL_ERROR, f"internal error: {type(e).__name__}: {e}")
        if reply is not None:
            # ensure_ascii keeps every message on one line whatever the package metadata contains.
            stdout.write(json.dumps(reply, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args in (["--version"], ["-V"]):
        print(f"wheelgap-mcp {__version__}")
        return 0
    if args:
        print(
            "usage: wheelgap-mcp\n\nAn MCP server for wheelgap over stdio. Add it to your agent's MCP settings with "
            "the command `uvx --from wheelgap wheelgap-mcp`.",
            file=sys.stderr,
        )
        return 0 if args in (["-h"], ["--help"]) else 2
    try:
        serve(sys.stdin.buffer, sys.stdout.buffer)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
