"""Fill in distribution filenames from the PyPI JSON API."""

from __future__ import annotations

import http.client
import json
import math
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import IO
from urllib.parse import quote, urlparse

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from . import __version__
from .sources import Package, SourceError

DEFAULT_INDEX = "https://pypi.org/pypi"


def index_url() -> str:
    # An empty value (a CI variable that isn't set) means the default, not a relative URL.
    return (os.environ.get("WHEELGAP_INDEX_URL") or DEFAULT_INDEX).rstrip("/")


def check_index_url() -> None:
    """Fail early, with a hint, for an index URL that can't work (pip's simple-index form)."""
    base = index_url()
    if base.endswith("/simple"):
        raise SourceError(
            f"WHEELGAP_INDEX_URL={base} is a simple index; wheelgap needs the JSON API, e.g. https://pypi.org/pypi"
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


def fetch_json(url: str, timeout: float = 20.0, attempts: int = 4) -> dict:
    """GET a JSON document, retrying only what can succeed later: 429, 5xx and dropped connections."""
    req = urllib.request.Request(url, headers={"User-Agent": f"wheelgap/{__version__}", "Accept": "application/json"})
    last: Exception | None = None
    for attempt in range(attempts):
        delay = 0.5 * 2**attempt
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                return json.load(resp)
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


def _files(entries: object) -> list[dict]:
    if not isinstance(entries, list) or not all(
        isinstance(e, dict) and isinstance(e.get("filename"), str) for e in entries
    ):
        raise ValueError("unexpected file list")
    return entries


def _newest_matching(data: dict, spec: str) -> tuple[str, list[dict]] | None:
    """The newest release that matches `spec` and has files that aren't yanked."""
    releases = data.get("releases")
    if not isinstance(releases, dict):
        # A mirror without the (deprecated) release list: fall back to the latest, if it matches.
        info, latest = data.get("info"), None
        if isinstance(info, dict):
            latest = info.get("version")
        live = [e for e in _files(data.get("urls", [])) if not e.get("yanked")]
        if isinstance(latest, str) and _in_spec(latest, spec) and live:
            return latest, live
        raise ValueError("no release list")  # reported as unknown, not as "can't install"
    usable = {}
    for text, entries in releases.items():
        try:
            live = [e for e in _files(entries) if not e.get("yanked")]
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


def fill(pkg: Package) -> Package:
    base = index_url()
    name = quote(pkg.key)
    pinned = bool(pkg.version)
    url = f"{base}/{name}/{quote(pkg.version or '')}/json" if pinned else f"{base}/{name}/json"
    try:
        data = fetch_json(url)
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
            found = _newest_matching(data, pkg.spec)
            if found is None:
                pkg.error = f"no release matches {pkg.spec}"
                return pkg
            pkg.version, urls = found
        else:
            urls = _files(data.get("urls", []))
            if not pinned:
                info = data.get("info")
                pkg.version = info.get("version") if isinstance(info, dict) else None
        names = [u["filename"] for u in urls]
        if pinned:
            # Keep yanked files but mark them: check.status_for uses them only if nothing else fits (PEP 592).
            pkg.yanked = frozenset(u["filename"] for u in urls if u.get("yanked"))
        else:
            names = [u["filename"] for u in urls if not u.get("yanked")]
        pkg.files = names
    except (ValueError, TypeError, KeyError, AttributeError):
        pkg.error = "unexpected response from the index"
    return pkg


PYPI_SIMPLE = "https://pypi.org/simple"


def recheck_lock(packages: list[Package]) -> tuple[int, int]:
    """Add the index's files to lockfile packages whose lock may be missing some (see pythons_outside_lock).

    Only packages the lock says came from PyPI are looked up. The lock's own files are kept, and a package
    whose lookup fails stays as the lock described it. Returns (packages from other indexes, failed lookups).
    """
    locked = [p for p in packages if not p.skip and p.files is not None and p.version]  # pinned: same release
    todo = [p for p in locked if (p.registry or PYPI_SIMPLE).rstrip("/") == PYPI_SIMPLE]
    saved = [p.files or [] for p in todo]
    fill_all(todo, refresh=True)
    failed = 0
    for p, lock in zip(todo, saved, strict=True):
        if p.error:
            p.files, p.error = lock, None
            failed += 1
        else:
            p.files = list(dict.fromkeys(lock + (p.files or [])))
    return len(locked) - len(todo), failed


def fill_all(packages: list[Package], refresh: bool = False, workers: int = 16) -> None:
    todo = [p for p in packages if not p.skip and (refresh or p.files is None)]
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(fill, todo))
