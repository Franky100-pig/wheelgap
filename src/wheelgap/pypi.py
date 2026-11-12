"""Fill in distribution filenames from the PyPI JSON API."""

from __future__ import annotations

import base64
import gzip
import hashlib
import http.client
import json
import math
import os
import posixpath
import tarfile
import time
import tomllib
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from contextvars import ContextVar
from functools import partial
from typing import IO
from urllib.parse import quote, unquote, urlparse, urlsplit, urlunsplit

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

from . import __version__, cache
from .sources import Package, SourceError

DEFAULT_INDEX = "https://pypi.org/pypi"


def index_url() -> str:
    # An empty value (a CI variable that isn't set) means the default, not a relative URL.
    return (os.environ.get("WHEELGAP_INDEX_URL") or DEFAULT_INDEX).rstrip("/")


def check_index_url() -> None:
    """Fail early, with a hint, for an index URL that can't work (pip's simple-index form)."""
    base = index_url()
    if base.endswith("/simple"):
        host = urlsplit(base).hostname or "the index"
        raise SourceError(
            f"WHEELGAP_INDEX_URL points at a simple index ({host}); wheelgap needs the JSON API, "
            "e.g. https://pypi.org/pypi. To use a simple index, pass --index-url instead."
        )


def _retry_after(e: urllib.error.HTTPError) -> float | None:
    try:
        value = float(e.headers.get("Retry-After", "")) if e.headers else None
    except ValueError:
        return None
    return value if value is not None and math.isfinite(value) and value >= 0 else None


class RedirectRefused(urllib.error.URLError):
    """An https:// request was redirected to a URL that isn't https://."""


class _HTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects that downgrade https:// to plain http:// (or another scheme)."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        if urlparse(req.full_url).scheme == "https" and urlparse(newurl).scheme != "https":
            fp.close()
            raise RedirectRefused(f"refused redirect from {req.full_url} to {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_HTTPSRedirectHandler)


def _request(req: urllib.request.Request, timeout: float, attempts: int, read):
    """Send `req`, retrying only what can succeed later: 429, 5xx and dropped connections."""
    last: Exception | None = None
    for attempt in range(attempts):
        delay = 0.5 * 2**attempt
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                return read(resp)
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                raise  # 404, 403, 410...: asking again won't help
            last = e
            delay = _retry_after(e) or delay
        except RedirectRefused:
            raise  # asking again gets the same redirect
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException, ConnectionError) as e:
            last = e  # includes connection resets and truncated responses
        if attempt + 1 < attempts:
            time.sleep(min(delay, 10.0))
    raise last  # type: ignore[misc]


def fetch_json(url: str, timeout: float = 20.0, attempts: int = 4) -> dict:
    """GET a JSON document (see _request for what is retried)."""
    req = urllib.request.Request(url, headers={"User-Agent": f"wheelgap/{__version__}", "Accept": "application/json"})
    return _request(req, timeout, attempts, json.load)


# --explain --build-info reads sdists (see build.py). Past any of these caps an sdist gives no answer.
SDIST_MAX_BYTES = 64 * 1024 * 1024  # downloaded (numpy's sdist is 19 MiB)
SDIST_MAX_UNPACKED = 512 * 1024 * 1024  # of tar unpacked from them
SDIST_MAX_HEADER = 1024 * 1024  # one pax or GNU long-name header, which tarfile reads whole into memory
PYPROJECT_MAX_BYTES = 1024 * 1024
_clock = time.monotonic  # deadlines are _clock() values; tests replace it


class SdistRefused(Exception):
    """No answer from this sdist, now or on a later try: past a cap, unreadable, or pip could read another
    pyproject.toml from it than wheelgap would (or would refuse to unpack it)."""


class SdistHashMismatch(Exception):
    """The download isn't the file whose sha256 the lock or the index gave."""


class DeadlineExceeded(Exception):
    """The time allowed for reading sdists ran out."""


class _Limited:
    """A read-only stream over `inner`, one network read at a time (read1, when it has one), so a slow server can't
    hold a read past `deadline`. Raises SdistRefused past `cap` bytes; `digest` sees every byte."""

    def __init__(self, inner, cap: int, deadline: float | None = None, digest=None):
        self._read = getattr(inner, "read1", inner.read)
        self._left, self._deadline, self._digest = cap, deadline, digest

    def read(self, n: int = -1) -> bytes:
        if self._deadline is not None and _clock() > self._deadline:
            raise DeadlineExceeded
        data = self._read(self._left + 1 if n < 0 else min(n, self._left + 1))
        if len(data) > self._left:
            raise SdistRefused("too big")
        self._left -= len(data)
        if self._digest is not None:
            self._digest.update(data)
        return data


class _TarInfo(tarfile.TarInfo):
    """Refuses what tarfile would otherwise hold in memory whatever its size: a pax or GNU long-name header past
    SDIST_MAX_HEADER, and the map of a sparse file (sdists have none)."""

    def _proc_member(self, tar):
        long_header = (tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK, tarfile.XHDTYPE, tarfile.XGLTYPE)
        if self.type in (*long_header, tarfile.SOLARIS_XHDTYPE) and self.size > SDIST_MAX_HEADER:
            raise SdistRefused("header too big")
        if self.type == tarfile.GNUTYPE_SPARSE:
            raise SdistRefused("sparse file")
        return super()._proc_member(tar)  # type: ignore[misc]

    def _proc_gnusparse_00(self, *args):  # pax sparse formats
        raise SdistRefused("sparse file")

    _proc_gnusparse_01 = _proc_gnusparse_10 = _proc_gnusparse_00


def _escapes(member: tarfile.TarInfo, parts: list[str], top: str) -> bool:
    """Whether a link points outside the unpacked sdist, which pip refuses (tarfile's data filter)."""
    if member.issym():
        if member.linkname.startswith("/"):
            return True
        target = posixpath.normpath(posixpath.join(posixpath.dirname("/".join(parts)) or ".", member.linkname))
    else:  # a hard link names an archive member; pip strips the top folder from it as from names
        lead, _, rest = member.linkname.lstrip("/").partition("/")
        if lead != top or not rest:
            return True
        target = posixpath.normpath(rest)
    return target == ".." or target.startswith("../")


def _root_pyproject(stream: _Limited) -> bytes | None:
    """The pyproject.toml pip finds at the root of the unpacked sdist, read from an uncompressed tar stream, or None.

    pip's untar_file strips the one folder every member shares, and the last member for a path wins. Wherever pip
    could end up with another file than this one, or would refuse the sdist, this raises SdistRefused: more than one
    top folder, a name with `..` or a backslash, a link out of the sdist or one a later member is written through,
    a device, or a pyproject.toml that isn't one plain file in lower case (macOS and Windows ignore case) with one
    content. The whole tar is read, but only that file is kept.
    """
    top: str | None = None
    links: set[str] = set()  # lower-cased paths of links seen so far
    found: bytes | None = None
    with tarfile.open(fileobj=stream, mode="r|", tarinfo=_TarInfo, encoding="utf-8") as tar:  # type: ignore[call-overload]
        while (member := tar.next()) is not None:
            tar.members.clear()  # stream mode keeps every header otherwise
            if "\\" in member.name or "\\" in member.linkname:  # a separator on Windows only
                raise SdistRefused("backslash in a name")
            lead, _, rest = member.name.lstrip("/").partition("/")
            if not lead or top not in (None, lead):
                raise SdistRefused("not one top folder")
            top = lead
            parts = [p for p in rest.split("/") if p not in ("", ".")]
            if ".." in parts:
                raise SdistRefused("'..' in a name")
            path = "/".join(parts).lower()
            if any("/".join(parts[:i]).lower() in links for i in range(1, len(parts) + 1)):
                raise SdistRefused("a member written through a link")
            if member.issym() or member.islnk():
                if not parts or _escapes(member, parts, lead):
                    raise SdistRefused("a link out of the sdist")
                links.add(path)
            elif member.isdev():
                raise SdistRefused("a device")
            if path != "pyproject.toml":
                continue
            if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or parts != ["pyproject.toml"]:
                raise SdistRefused("pyproject.toml isn't a plain file")
            if member.size > PYPROJECT_MAX_BYTES:
                raise SdistRefused("pyproject.toml too big")
            f = tar.extractfile(member)
            data = f.read() if f else b""
            if found is not None and data != found:
                raise SdistRefused("two different pyproject.toml")
            found = data
    return found


def fetch_sdist_pyproject(
    url: str, sha256: str | None = None, deadline: float | None = None, timeout: float = 10.0
) -> dict | None:
    """The parsed pyproject.toml at the root of the .tar.gz sdist at `url`, as pip would read it, or None if it has
    none.

    The whole sdist is streamed in memory, never to disk, and checked against `sha256` when given. Raises
    SdistRefused for an answer that won't change (see _root_pyproject, and the SDIST_MAX_* caps), SdistHashMismatch,
    DeadlineExceeded past `deadline`, or the network error. One attempt only, with no retries, and https only, with
    fetch_json's redirect rules. .zip sdists aren't read (ValueError).
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.path.endswith(".tar.gz"):
        raise ValueError("only https:// .tar.gz sdists are read")
    req = urllib.request.Request(url, headers={"User-Agent": f"wheelgap/{__version__}"})
    if deadline is not None:
        timeout = max(0.1, min(timeout, deadline - _clock()))

    def read(resp) -> bytes | None:
        digest = hashlib.sha256()
        packed = _Limited(resp, SDIST_MAX_BYTES, deadline, digest)
        try:
            with gzip.GzipFile(fileobj=packed, mode="rb") as unpacked:  # type: ignore[call-overload]
                found = _root_pyproject(_Limited(unpacked, SDIST_MAX_UNPACKED, deadline))
        except (tarfile.TarError, gzip.BadGzipFile, zlib.error) as e:
            raise SdistRefused(f"not a .tar.gz ({type(e).__name__})") from None
        if sha256 is not None:
            while packed.read(1 << 16):  # the rest of the file, for the hash
                pass
            if digest.hexdigest() != sha256.lower():
                raise SdistHashMismatch
        return found

    raw = _request(req, timeout, 1, read)
    if raw is None:
        return None
    try:  # as pip reads it: UTF-8 text with universal newlines
        return tomllib.loads(raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise SdistRefused(f"invalid pyproject.toml ({type(e).__name__})") from None


SIMPLE_JSON = "application/vnd.pypi.simple.v1+json"
# HTML is accepted at a token q so an HTML-only index answers with HTML (and a clear error) instead of a 406.
SIMPLE_ACCEPT = f"{SIMPLE_JSON}, text/html;q=0.01"
LOOPBACK = ("localhost", "127.0.0.1", "::1")


class IndexFormatError(Exception):
    """The index answered, but not with the JSON simple API. Carries only the host, never the URL."""

    def __init__(self, host: str):
        super().__init__(f"{host} only serves the HTML simple API; wheelgap needs the JSON form (PEP 691)")


def split_credentials(url: str) -> tuple[str, str | None]:
    """('https://user:tok@host/simple') -> ('https://host/simple', 'Basic ...').

    The secret leaves the URL here, so nothing downstream can print it.
    """
    if not url.strip():
        raise SourceError("--index-url is empty (is a CI variable unset?)")
    try:
        parts = urlsplit(url)
        host, port, user, password = parts.hostname, parts.port, parts.username, parts.password
    except ValueError:
        raise SourceError("--index-url is not a valid URL") from None  # don't echo it back
    if parts.scheme not in ("http", "https") or not host:
        raise SourceError("--index-url must be an http(s) URL")
    if parts.scheme == "http" and host not in LOOPBACK:
        raise SourceError("--index-url must use https (plain http is only allowed for localhost)")
    netloc = f"[{host}]" if ":" in host else host
    if port:
        netloc += f":{port}"
    clean = urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))
    auth = None
    if user is not None or password is not None:
        raw = f"{unquote(user or '')}:{unquote(password or '')}".encode()
        auth = "Basic " + base64.b64encode(raw).decode()
    return clean, auth


def fetch_simple(url: str, auth: str | None = None, timeout: float = 20.0, attempts: int = 4) -> dict:
    """GET a PEP 691 project page. `auth` is an Authorization header value (see split_credentials)."""
    req = urllib.request.Request(url, headers={"User-Agent": f"wheelgap/{__version__}", "Accept": SIMPLE_ACCEPT})
    if auth:
        # Sent on this request only: urllib doesn't copy unredirected headers onto a redirect,
        # so the token can't follow a redirect to another host.
        req.add_unredirected_header("Authorization", auth)
    host = urlsplit(url).hostname or "index"

    def read(resp):
        kind = resp.headers.get_content_type()
        if kind != "application/json" and not kind.endswith("+json"):
            raise IndexFormatError(host)  # text/html: PEP 503 only
        return json.load(resp)

    return _request(req, timeout, attempts, read)


def _is_yanked(entry: dict) -> bool:
    y = entry.get("yanked", False)  # PEP 592/691: true, or a reason string
    return y is True or isinstance(y, str)


def _parse_filename(filename: str):
    try:
        if filename.endswith(".whl"):
            name, version, *_ = parse_wheel_filename(filename)
        else:
            name, version = parse_sdist_filename(filename)
    except (InvalidWheelFilename, InvalidSdistFilename, InvalidVersion):
        return None  # skip files that don't parse
    return name, version


def _group_by_version(data: object, key: str) -> dict[Version, list[dict]]:
    if not isinstance(data, dict):
        raise ValueError("not an object")
    target = canonicalize_name(key)
    grouped: dict[Version, list[dict]] = {}
    for entry in _files(data.get("files")):
        parsed = _parse_filename(entry["filename"])
        if parsed and parsed[0] == target:
            grouped.setdefault(parsed[1], []).append(entry)
    return grouped


def fill_simple(pkg: Package, base: str, auth: str | None, python: str | None = None) -> Package:
    try:
        data = fetch_simple(f"{base}/{quote(pkg.key)}/", auth)
    except urllib.error.HTTPError as e:
        pkg.error = "not on index" if e.code == 404 else f"HTTP {e.code}"
        return pkg
    except IndexFormatError as e:
        pkg.error = str(e)
        return pkg
    except Exception as e:  # never put str(e) here: it can contain the URL
        pkg.error = f"lookup failed ({type(e).__name__})"
        return pkg
    try:
        grouped = _group_by_version(data, pkg.key)
        if pkg.version:  # pinned: that release's files; yanked ones are kept but marked (PEP 592)
            entries = grouped.get(Version(pkg.version))
            if not entries:
                pkg.error = f"version {pkg.version} not on index"
                return pkg
            pkg.files = [e["filename"] for e in entries]
            pkg.yanked = frozenset(e["filename"] for e in entries if _is_yanked(e))
            return pkg

        # A range, or no pin: the newest matching release that isn't fully yanked. As in fill(), a range only
        # counts files whose Requires-Python admits `python`.
        def usable(e: dict) -> bool:
            return not _is_yanked(e) and (not pkg.spec or _supports_python(e, python, "requires-python"))

        live = {v: kept for v, es in grouped.items() if (kept := [e for e in es if usable(e)])}
        chosen = max(SpecifierSet(pkg.spec or "").filter(live), default=None)
        if chosen is None:
            if pkg.spec:
                pkg.error = f"no release matches {pkg.spec}" + (f" for Python {python}" if python else "")
            else:
                pkg.error = "not on index"
            return pkg
        pkg.version, pkg.version_from_index = str(chosen), True
        pkg.files = [e["filename"] for e in live[chosen]]
    except (ValueError, TypeError, KeyError, AttributeError):  # InvalidVersion is a ValueError
        pkg.error = "unexpected response from the index"
    return pkg


def _files(entries: object) -> list[dict]:
    if not isinstance(entries, list) or not all(
        isinstance(e, dict) and isinstance(e.get("filename"), str) for e in entries
    ):
        raise ValueError("unexpected file list")
    return entries


def _supports_python(entry: dict, python: str | None, key: str = "requires_python") -> bool:
    """Missing or invalid Requires-Python metadata imposes no restriction. PEP 691 spells the key requires-python."""
    requires = entry.get(key)
    if python is None or not isinstance(requires, str):
        return True
    try:
        # A real interpreter is on a later patch release than x.y.0 (pip compares sys.version_info[:3]),
        # so `>=3.8.1` must admit 3.8 and `!=3.9.0` must admit 3.9: compare against a late patch.
        return SpecifierSet(requires).contains(f"{python}.99")
    except InvalidSpecifier:
        return True


def _newest_matching(data: dict, spec: str, python: str | None = None) -> tuple[str, list[dict]] | None:
    """The newest matching release with non-yanked files supporting `python`, when supplied."""
    releases = data.get("releases")
    if not isinstance(releases, dict):
        # A mirror without the (deprecated) release list: fall back to the latest, if it matches.
        info, latest = data.get("info"), None
        if isinstance(info, dict):
            latest = info.get("version")
        live = [e for e in _files(data.get("urls", [])) if not e.get("yanked") and _supports_python(e, python)]
        if isinstance(latest, str) and _in_spec(latest, spec) and live:
            return latest, live
        raise ValueError("no release list")  # reported as unknown, not as "can't install"
    usable = {}
    for text, entries in releases.items():
        try:
            live = [e for e in _files(entries) if not e.get("yanked") and _supports_python(e, python)]
            if live:
                usable[Version(text)] = (text, live)
        except (InvalidVersion, ValueError):
            continue
    chosen = max(SpecifierSet(spec).filter(usable), default=None)
    return usable[chosen] if chosen is not None else None


def _in_spec(version: str, spec: str) -> bool:
    try:
        return SpecifierSet(spec).contains(version, prereleases=True)
    except (InvalidVersion, ValueError):
        return False


def _not_found(base: str, name: str, version: str | None) -> str:
    if version:
        try:  # is it the version that's missing, or the whole project?
            fetch_json(f"{base}/{name}/json")
            return f"version {version} not on index"
        except urllib.error.HTTPError as e:
            if e.code != 404:
                return f"version {version} not on index (couldn't check the project: HTTP {e.code})"
        except Exception as e:
            return f"version {version} not on index (couldn't check the project: {type(e).__name__})"
    return "not on index"


def fill(pkg: Package, python: str | None = None, read_cache: bool = False, write_cache: bool = False) -> Package:
    base = index_url()
    name = quote(pkg.key)
    pinned = bool(pkg.version)
    url = f"{base}/{name}/{quote(pkg.version or '')}/json" if pinned else f"{base}/{name}/json"
    # Only a pinned release is cached: the latest release, or a range's pick, can change at any upload.
    key = (base, pkg.key, pkg.version) if pkg.version else None
    cached = cache.load_files(*key) if key and read_cache else None
    try:
        data = {"urls": cached} if cached is not None else fetch_json(url)
    except urllib.error.HTTPError as e:
        pkg.error = _not_found(base, name, pkg.version) if e.code == 404 else f"HTTP {e.code}"
        return pkg
    except Exception as e:  # network trouble shouldn't kill the whole report
        pkg.error = f"lookup failed ({type(e).__name__})"
        return pkg
    try:
        if not isinstance(data, dict):
            raise ValueError("not an object")
        if not pinned and pkg.spec:
            # A range (numpy>=1.26,<2): check the release an installer would pick, not the latest.
            found = _newest_matching(data, pkg.spec, python)
            if found is None:
                pkg.error = f"no release matches {pkg.spec}" + (f" for Python {python}" if python else "")
                return pkg
            pkg.version, urls = found
            pkg.version_from_index = True
        else:
            urls = _files(data.get("urls", []))
            if not pinned:
                info = data.get("info")
                pkg.version = info.get("version") if isinstance(info, dict) else None
                pkg.version_from_index = True
        names = [u["filename"] for u in urls]
        if pinned:
            # Keep yanked files but mark them: check.status_for uses them only if nothing else fits (PEP 592).
            pkg.yanked = frozenset(u["filename"] for u in urls if u.get("yanked"))
        else:
            names = [u["filename"] for u in urls if not u.get("yanked")]
        pkg.files = names
        pkg.looked_up_on = base
        # Only a fresh answer that parsed and listed its files: a body with no "urls" ({}) reads as no files,
        # and caching that would turn a bad response into a day of false "missing" answers.
        if key and write_cache and cached is None and isinstance(data.get("urls"), list):
            cache.save_files(*key, urls)
    except (ValueError, TypeError, KeyError, AttributeError):
        pkg.error = "unexpected response from the index"
    return pkg


PYPI_SIMPLE = "https://pypi.org/simple"


def is_pypi(registry: str | None) -> bool:
    """Whether a package pinned to `registry` comes from PyPI: unset (the default index) or exactly PYPI_SIMPLE."""
    return (registry or PYPI_SIMPLE).rstrip("/") == PYPI_SIMPLE


def _index(url: str, suffix: str) -> tuple[str, str, int | None, str] | None:
    """(scheme, hostname, port, repository path) of an index URL ending in `suffix`, the default port filled in.
    One host can serve many repositories (Artifactory, Nexus), so the path counts. Never the user or password.
    None for anything that isn't plainly such a URL, so it matches nothing: that includes a backslash, whitespace or
    a control character, which other URL parsers (pip's, a browser's) may read as a different host."""
    if "\\" in url or any(c.isspace() or not c.isprintable() for c in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:  # a malformed host or port
        return None
    path = parts.path.rstrip("/")
    if not parts.hostname or port == 0 or not path.endswith(suffix) or parts.query or parts.fragment:
        return None
    return parts.scheme, parts.hostname, port or {"https": 443, "http": 80}.get(parts.scheme), path[: -len(suffix)]


def may_look_up(registry: str | None, simple_index: str | None = None) -> bool:
    """Whether a package pinned to `registry` may be looked up by name: it comes from PyPI, or, for lookups on the
    JSON API (no `simple_index`), from the index WHEELGAP_INDEX_URL points at, such as a PyPI mirror: the same
    scheme, host, port and repository path (`<repo>/simple` and `<repo>/pypi`). Either way its name only goes to the
    index the lock says it came from. Unset, WHEELGAP_INDEX_URL is PyPI and only is_pypi() counts."""
    if is_pypi(registry):
        return True
    base = index_url()
    if registry is None or simple_index is not None or base == DEFAULT_INDEX:
        return False
    index = _index(registry, "/simple")
    return index is not None and index == _index(base, "/pypi")


def keeps_locked_files(p: Package) -> bool:
    """Under --online, a package from another index keeps the files in the lock: PyPI doesn't know it."""
    return not p.skip and p.files is not None and not is_pypi(p.registry)


def recheck_lock(
    packages: list[Package], simple_index: str | None = None, read_cache: bool = False, write_cache: bool = False
) -> tuple[int, int]:
    """Add the index's files to lockfile packages whose lock may be missing some (see pythons_outside_lock).

    Only packages the lock says came from PyPI are looked up. The lock's own files are kept, and a package
    whose lookup fails stays as the lock described it. Returns (packages from other indexes, failed lookups).
    """
    locked = [p for p in packages if not p.skip and p.files is not None and p.version]  # pinned: same release
    todo = [p for p in locked if is_pypi(p.registry)]
    saved = [p.files or [] for p in todo]
    fill_all(todo, refresh=True, simple_index=simple_index, read_cache=read_cache, write_cache=write_cache)
    failed = 0
    for p, lock in zip(todo, saved, strict=True):
        if p.error:
            p.files, p.error = lock, None
            failed += 1
        else:
            p.files = list(dict.fromkeys(lock + (p.files or [])))
    return len(locked) - len(todo), failed


Progress = Callable[[int, int], None]
"""Called as progress(done, total) by fill_all: (0, total) before the first lookup, then once per finished one."""

_progress: ContextVar[Progress | None] = ContextVar("wheelgap_progress", default=None)


@contextmanager
def reporting_progress(progress: Progress) -> Iterator[None]:
    """Make every fill_all in this thread (or context) report to `progress`. A context rather than an argument, so
    the MCP server's check_lockfile gets progress through cli.main without changing the CLI or the API."""
    token = _progress.set(progress)
    try:
        yield
    finally:
        _progress.reset(token)


def fill_all(
    packages: list[Package],
    refresh: bool = False,
    workers: int = 16,
    python: str | None = None,
    simple_index: str | None = None,
    read_cache: bool = False,
    write_cache: bool = False,
) -> int:
    """Look packages up on PyPI. A package the lock says came from another index is never looked up: PyPI doesn't
    know it. With refresh (--online) it keeps its locked files; with no files (an old poetry.lock, a Pipfile.lock)
    it is skipped as a private index, unless WHEELGAP_INDEX_URL is that same index (see may_look_up).
    `python` limits range resolution to releases supporting that interpreter.
    With `simple_index` (--index-url), lookups go to that PEP 691 simple index instead of PyPI's JSON API.
    `read_cache` and `write_cache` read pinned JSON API lookups from, and save them to, the on-disk cache (see
    cache.py); simple-index lookups never use it.
    Inside reporting_progress, it reports to that callback as lookups finish.
    Returns how many kept their locked files for that reason."""
    fn = partial(fill, python=python, read_cache=read_cache, write_cache=write_cache)
    if simple_index is not None:
        base, auth = split_credentials(simple_index)  # validate first: a bad URL fails before any lookup
        fn = partial(fill_simple, base=base, auth=auth, python=python)
    for p in packages:
        if p.files is None and not p.skip and not may_look_up(p.registry, simple_index):
            p.skip = "private index"  # the name is internal; a namesake on PyPI is another package
    kept = {id(p) for p in packages if refresh and keeps_locked_files(p)}
    # An error from parsing (a constraint wheelgap can't apply) stays unknown: a lookup would hide it.
    todo = [p for p in packages if not p.skip and not p.error and id(p) not in kept and (refresh or p.files is None)]
    report = _progress.get()
    if todo:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            if report is None:
                list(pool.map(fn, todo))
            else:
                report(0, len(todo))
                for done, future in enumerate(as_completed([pool.submit(fn, p) for p in todo]), 1):
                    future.result()
                    report(done, len(todo))
    return len(kept)
