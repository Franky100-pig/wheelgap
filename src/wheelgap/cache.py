"""A small on-disk cache of pinned index lookups (/<name>/<version>/json), so a repeat run needn't ask again, and of
what a release's sdist needs to build (--explain's build info).

A cache that can't be read or written is only ever a miss: it never fails a run.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

MAX_AGE = 24 * 60 * 60  # seconds: new wheels for an existing release show up within a day
PRUNE_EVERY = 24 * 60 * 60  # seconds between two prunes of expired entries
# The only files prune() and clear() ever delete: entries (_path's names) and leftovers of an interrupted save.
_ENTRY = re.compile(r"[0-9a-f]{64}\.json", re.ASCII)
_LEFTOVER = re.compile(r"\.tmp-[^/\\]*\.json")


def _now() -> float:
    return time.time()


def cache_dir() -> Path:
    """The platform's cache folder, as pip and uv choose it (without platformdirs)."""
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        base = local if os.path.isabs(local) else str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Caches")
    else:
        xdg = os.environ.get("XDG_CACHE_HOME", "")
        base = xdg if os.path.isabs(xdg) else str(Path.home() / ".cache")  # the XDG spec ignores relative paths
    return Path(base) / "wheelgap" / "pinned-v1"


def _path(index: str, name: str, version: str, kind: str = "") -> Path:
    # Credentials in the index URL (WHEELGAP_INDEX_URL=https://user:token@host/pypi) are left out of the key,
    # and what's left is hashed, so no URL is written in clear text. `kind` keeps other entries for the same
    # release (build info) apart from its file list.
    parts = urlsplit(index)
    index = urlunsplit(parts._replace(netloc=parts.netloc.rpartition("@")[2]))
    key = f"{index}\n{name}\n{version}" + (f"\n{kind}" if kind else "")
    return cache_dir() / f"{hashlib.sha256(key.encode()).hexdigest()}.json"


def _fresh(index: str, name: str, version: str, kind: str = "") -> dict | None:
    """The entry if it's an object saved less than MAX_AGE ago, else None (a miss)."""
    try:
        entry = json.loads(_path(index, name, version, kind).read_text(encoding="utf-8"))
        saved = entry["saved"]
        if isinstance(saved, bool) or not isinstance(saved, int | float) or not 0 <= _now() - saved < MAX_AGE:
            return None
        return entry
    except Exception:  # missing, corrupt or unreadable: a miss
        return None


def load_files(index: str, name: str, version: str) -> list[dict] | None:
    """The cached file list of a pinned release, or None: not cached, older than MAX_AGE, or unreadable."""
    entry = _fresh(index, name, version)
    files = entry.get("files") if entry else None
    if isinstance(files, list) and all(isinstance(f, dict) and isinstance(f.get("filename"), str) for f in files):
        return files
    return None


def save_files(index: str, name: str, version: str, files: list[dict]) -> None:
    """Store a pinned release's file list. Only `filename` and `yanked` are kept: they're all fill() reads,
    and a file's URL (which a private mirror may sign) never reaches the disk."""
    kept = [{k: f[k] for k in ("filename", "yanked") if k in f} for f in files]
    _save(index, name, version, "", {"files": kept})


def load_build(index: str, name: str, version: str) -> dict | None:
    """A release's cached build info, {"requires": [...] or None, "build_script": bool}, or None for a miss."""
    entry = _fresh(index, name, version, "build")
    if entry is None:
        return None
    requires, script = entry.get("requires"), entry.get("build_script")
    if (requires is None or (isinstance(requires, list) and all(isinstance(r, str) for r in requires))) and (
        isinstance(script, bool)
    ):
        return {"requires": requires, "build_script": script}
    return None


def save_build(index: str, name: str, version: str, requires: list[str] | None, build_script: bool) -> None:
    """Store what a release's sdist says it needs to build (see build.py)."""
    _save(index, name, version, "build", {"requires": requires, "build_script": build_script})


def _save(index: str, name: str, version: str, kind: str, data: dict) -> None:
    try:
        path = _path(index, name, version, kind)
        path.parent.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # private to the user (not on Windows)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        # Written next to the target, then renamed over it, so a reader never sees half a file.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"saved": _now(), **data}, f)
            os.utime(tmp, (_now(), _now()))  # prune() reads the age from the mtime
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except Exception:  # read-only home, full disk, a race on Windows...: the next run just looks it up again
        pass
    prune()


def _own_folder(folder: Path) -> bool:
    """The cache folder is a real directory, not a symlink or Windows junction to somewhere else."""
    try:
        info = os.lstat(folder)
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        return False
    # Python 3.11 on Windows reports a junction as a plain folder; its reparse tag gives it away.
    tag = getattr(info, "st_reparse_tag", 0)  # Windows only
    if tag and tag == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1):
        return False
    isjunction = getattr(os.path, "isjunction", None)  # Python 3.12+
    return not (isjunction is not None and isjunction(folder))


def _own_files(folder: Path) -> Iterator[os.DirEntry[str]]:
    """Regular files wheelgap wrote in the folder: entries and save leftovers. Links are never followed."""
    with os.scandir(folder) as entries:
        for entry in entries:
            if (_ENTRY.fullmatch(entry.name) or _LEFTOVER.fullmatch(entry.name)) and entry.is_file(
                follow_symlinks=False
            ):
                yield entry


def prune(max_age: float = MAX_AGE) -> None:
    """Delete entries older than max_age, which load_files would ignore anyway. Runs at most once a day:
    a `last-prune` marker next to the entries holds the time of the last run in its mtime. Only wheelgap's
    own files are touched, never through a link, and nothing here ever fails a run."""
    try:
        folder = cache_dir()
        if not _own_folder(folder):
            return
        marker = folder / "last-prune"
        if marker.is_symlink():
            return
        with contextlib.suppress(FileNotFoundError):
            if 0 <= _now() - marker.stat().st_mtime < PRUNE_EVERY:
                return
        marker.touch(mode=0o600)
        os.utime(marker, (_now(), _now()))
        for entry in _own_files(folder):
            with contextlib.suppress(OSError):  # another run may have replaced or removed it
                if _now() - entry.stat(follow_symlinks=False).st_mtime >= max_age:
                    os.unlink(entry.path)
    except Exception:  # no folder yet, one we can't list, no home folder at all...: try again next time
        pass


def clear() -> tuple[Path, int]:
    """--clear-cache: delete wheelgap's files in the cache folder, then the folder if that leaves it empty.
    Returns the folder and how many entries were removed. A folder that's a link is refused, not followed."""
    folder = cache_dir()
    if not folder.exists() and not folder.is_symlink():
        return folder, 0
    if not _own_folder(folder):
        raise OSError(f"{folder} is a link or not a folder, so wheelgap won't clear it; remove it yourself")
    count = 0
    for entry in _own_files(folder):
        with contextlib.suppress(FileNotFoundError):  # another run may have removed it
            os.unlink(entry.path)
            count += bool(_ENTRY.fullmatch(entry.name))
    with contextlib.suppress(FileNotFoundError):
        (folder / "last-prune").unlink()  # a link here is removed itself, not its target
    with contextlib.suppress(OSError):  # not empty: another run saved meanwhile, or someone else's file
        folder.rmdir()
    return folder, count
