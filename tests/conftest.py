from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

# Fake PyPI: name -> {version: [filenames]}, plus the "latest" version for unpinned lookups.
FAKE_INDEX = {
    "fastlib": {"2.0.0": ["fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl", "fastlib-2.0.0.tar.gz"]},
    "unpinned": {"4.2": ["unpinned-4.2-py3-none-any.whl"]},
    "winonly": {"311": ["winonly-311-cp313-cp313-win_amd64.whl"]},
    "purewheel": {"3.1": ["purewheel-3.1-py3-none-any.whl"]},
    "nowheels": {"0.9": ["nowheels-0.9.tar.gz"]},
    "yanked": {"1.0": [{"filename": "yanked-1.0-py3-none-any.whl", "yanked": True}]},
    "halfyanked": {"2.0": [{"filename": "halfyanked-2.0-py3-none-any.whl", "yanked": True}, "halfyanked-2.0.tar.gz"]},
}


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture
def fake_pypi(monkeypatch):
    import urllib.error

    from wheelgap import pypi

    calls = []

    def fetch_json(url, timeout=20.0):
        calls.append(url)
        parts = url.removeprefix(pypi.DEFAULT_INDEX + "/").removesuffix("/json").split("/")
        name = parts[0]
        if name not in FAKE_INDEX:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        versions = FAKE_INDEX[name]
        version = parts[1] if len(parts) > 1 else next(reversed(versions))
        files = versions.get(version)
        if files is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        urls = [f if isinstance(f, dict) else {"filename": f} for f in files]
        return {"info": {"version": version}, "urls": urls}

    monkeypatch.setattr(pypi, "fetch_json", fetch_json)
    return calls
