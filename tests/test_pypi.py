import io
import json
import urllib.error

import pytest

import wheelgap
from wheelgap import Status, pypi
from wheelgap.sources import Package, SourceError, load_inputs


def test_version_range_checks_the_newest_matching_release(fake_pypi, tmp_path):
    # ranged has 1.5 (wheel), 1.9 (sdist only) and 2.0 (wheel); "<2" must check 1.9, not 2.0.
    (tmp_path / "requirements.txt").write_text("ranged>=1.5,<2\n", encoding="utf-8")
    (src,) = load_inputs([str(tmp_path / "requirements.txt")])
    pypi.fill_all(src.packages)
    (pkg,) = src.packages
    assert (pkg.version, pkg.files) == ("1.9", ["ranged-1.9.tar.gz"])


def test_version_range_with_no_match(fake_pypi):
    pkg = Package("ranged", None, spec=">=3")
    pypi.fill(pkg)
    assert pkg.error == "no release matches >=3"


def test_empty_index_url_means_pypi(monkeypatch):
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "")
    assert pypi.index_url() == pypi.DEFAULT_INDEX


def test_simple_index_url_is_a_clear_error(monkeypatch):
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://pypi.org/simple/")
    with pytest.raises(SourceError, match="needs the JSON API"):
        pypi.check_index_url()


@pytest.mark.parametrize("response", [None, [], {"urls": "x"}, {"urls": [{"name": "no-filename.whl"}]}])
def test_odd_index_responses_are_unknown_not_a_crash(monkeypatch, response):
    monkeypatch.setattr(pypi, "fetch_json", lambda url, timeout=20.0: response)
    pkg = pypi.fill(Package("x", "1.0"))
    assert pkg.error == "unexpected response from the index" and pkg.files is None


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_json_retries_transient_failures_only(monkeypatch):
    sleeps, calls = [], []
    monkeypatch.setattr(pypi.time, "sleep", sleeps.append)

    def urlopen(req, timeout):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise ConnectionResetError("reset by peer")  # a dropped connection is worth a retry
        if len(calls) == 2:
            raise urllib.error.HTTPError(req.full_url, 503, "busy", {"Retry-After": "2"}, None)
        return FakeResponse(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(pypi.urllib.request, "urlopen", urlopen)
    assert pypi.fetch_json("https://pypi.org/pypi/x/json") == {"ok": True}
    assert sleeps == [0.5, 2.0]  # backoff, then the server's Retry-After

    calls.clear()

    def forbidden(req, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(req.full_url, 403, "no", {}, None)

    monkeypatch.setattr(pypi.urllib.request, "urlopen", forbidden)
    with pytest.raises(urllib.error.HTTPError):
        pypi.fetch_json("https://pypi.org/pypi/x/json")
    assert calls == [1]  # a 403 won't change on retry


def test_pinned_version_missing_is_named(fake_pypi):
    targets = wheelgap.make_targets(python="3.13", platforms="linux")
    result = wheelgap.check_package("fastlib", "9.9", targets)
    assert result.error == "version 9.9 not on index" and result.statuses[targets[0]] is Status.UNKNOWN


def test_range_on_a_mirror_without_a_release_list(monkeypatch):
    data = {"info": {"version": "2.0"}, "urls": [{"filename": "x-2.0-py3-none-any.whl"}]}
    monkeypatch.setattr(pypi, "fetch_json", lambda url, timeout=20.0: data)
    assert pypi.fill(Package("x", None, spec=">=1")).version == "2.0"  # latest matches: fine
    assert pypi.fill(Package("x", None, spec="<2")).error == "unexpected response from the index"


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "soon"])
def test_bad_retry_after_values_are_ignored(value):
    err = urllib.error.HTTPError("u", 503, "busy", {"Retry-After": value}, None)
    assert pypi._retry_after(err) is None


def test_missing_version_when_the_project_check_fails(monkeypatch):
    def fetch(url, timeout=20.0):
        code = 404 if "/1.0/" in url else 503
        raise urllib.error.HTTPError(url, code, "x", {}, None)

    monkeypatch.setattr(pypi, "fetch_json", fetch)
    assert pypi.fill(Package("x", "1.0")).error == "version 1.0 not on index (couldn't check the project: HTTP 503)"


def test_mirror_fallback_with_no_usable_files_is_unknown(monkeypatch):
    data = {"info": {"version": "2.0"}, "urls": [{"filename": "x-2.0.tar.gz", "yanked": True}]}
    monkeypatch.setattr(pypi, "fetch_json", lambda url, timeout=20.0: data)
    pkg = pypi.fill(Package("x", None, spec=">=1"))
    assert pkg.error == "unexpected response from the index" and pkg.files is None
