"""Fill in distribution filenames from the PyPI JSON API."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from . import __version__
from .sources import Package

DEFAULT_INDEX = "https://pypi.org/pypi"


def index_url() -> str:
    return os.environ.get("WHEELGAP_INDEX_URL", DEFAULT_INDEX).rstrip("/")


def fetch_json(url: str, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": f"wheelgap/{__version__}", "Accept": "application/json"})
    last: Exception | None = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            last = e
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
    raise last  # type: ignore[misc]


def fill(pkg: Package) -> Package:
    base = index_url()
    name = quote(pkg.key)
    url = f"{base}/{name}/{quote(pkg.version)}/json" if pkg.version else f"{base}/{name}/json"
    try:
        data = fetch_json(url)
    except urllib.error.HTTPError as e:
        pkg.error = "not on index" if e.code == 404 else f"HTTP {e.code}"
        return pkg
    except Exception as e:  # network trouble shouldn't kill the whole report
        pkg.error = f"lookup failed ({type(e).__name__})"
        return pkg
    pinned = bool(pkg.version)
    if not pinned:
        pkg.version = data.get("info", {}).get("version")
    urls = data.get("urls", [])
    # PEP 592: installers skip yanked files, except for a release pinned with == whose files are all
    # yanked (as pip does), so only then do the yanked files count.
    all_yanked = bool(urls) and all(u.get("yanked") for u in urls)
    pkg.files = [u["filename"] for u in urls if not u.get("yanked") or (pinned and all_yanked)]
    return pkg


def fill_all(packages: list[Package], refresh: bool = False, workers: int = 16) -> None:
    todo = [p for p in packages if not p.skip and (refresh or p.files is None)]
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(fill, todo))
