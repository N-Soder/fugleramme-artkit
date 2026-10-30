import io
import json

import pytest
from PIL import Image
from test_sources import approve, record

from fugart import fetch, sources, store


class Response(io.BytesIO):
    def __init__(self, body, declared_size=None, url="https://archive.org/scan.jpg"):
        super().__init__(body)
        self.headers = {
            "Content-Type": "image/png",
            "Content-Length": str(len(body) if declared_size is None else declared_size),
        }
        self.url = url

    def geturl(self):
        return self.url


def png():
    data = io.BytesIO()
    Image.new("RGB", (800, 700), "white").save(data, "PNG")
    return data.getvalue()


def test_fetch_requires_human_approval_before_network(source_workspace, monkeypatch):
    workspace, path = source_workspace
    record(workspace, path)
    monkeypatch.setattr(fetch, "open_remote", lambda _: pytest.fail("unapproved network request"))
    with pytest.raises(ValueError, match="approval"):
        fetch.acquire(workspace, "Welcome Swallow")
    assert not (workspace / "work/scans").exists()


def test_fetch_is_content_addressed_and_retry_checks_original_bytes(source_workspace, monkeypatch):
    workspace, path = source_workspace
    record(workspace, path)
    approve(workspace)
    monkeypatch.setattr(fetch, "open_remote", lambda _: Response(png()))
    result = fetch.acquire(workspace, "Welcome Swallow")
    assert (result["width"], result["height"], result["format"]) == (800, 700, "PNG")
    scan = workspace / result["path"]
    assert scan.read_bytes() == png()
    assert scan.stem == result["sha256"]
    monkeypatch.setattr(fetch, "open_remote", lambda _: pytest.fail("retry must not refetch"))
    assert fetch.acquire(workspace, "Welcome Swallow")["changed"] is False
    scan.write_text("corrupt cached scan")
    with pytest.raises(ValueError, match="checksum"):
        fetch.acquire(workspace, "Welcome Swallow")


@pytest.mark.parametrize(
    "body,declared", [(b"<html>not an image</html>", None), (png(), len(png()) + 100)]
)
def test_failed_download_never_advances_or_promotes_partial_scan(
    source_workspace, monkeypatch, body, declared
):
    workspace, path = source_workspace
    record(workspace, path)
    approve(workspace)
    monkeypatch.setattr(fetch, "open_remote", lambda _: Response(body, declared))
    with pytest.raises((ValueError, OSError)):
        fetch.acquire(workspace, "Welcome Swallow")
    with store.connect(workspace, readonly=True) as db:
        assert store.find_species(db, "Welcome Swallow")["state"] == "sourced"
        assert db.execute("SELECT COUNT(*) FROM downloads").fetchone()[0] == 0
    assert not list((workspace / "work/scans").glob("*.png"))


def test_verified_download_recovers_after_promotion_before_database_commit(
    source_workspace, monkeypatch
):
    workspace, path = source_workspace
    record(workspace, path)
    approve(workspace)
    monkeypatch.setattr(fetch, "open_remote", lambda _: Response(png()))
    complete = fetch.complete
    monkeypatch.setattr(
        fetch, "complete", lambda *args: (_ for _ in ()).throw(RuntimeError("simulated crash"))
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        fetch.acquire(workspace, "Welcome Swallow")
    assert len(list((workspace / "work/scans").glob("*.png"))) == 1
    monkeypatch.setattr(fetch, "complete", complete)
    monkeypatch.setattr(fetch, "open_remote", lambda _: pytest.fail("recovery must not refetch"))
    assert fetch.acquire(workspace, "Welcome Swallow")["recovered"] is True


def test_changing_source_retains_old_scan_but_requires_new_approval(source_workspace, monkeypatch):
    workspace, path = source_workspace
    record(workspace, path)
    approve(workspace)
    monkeypatch.setattr(fetch, "open_remote", lambda _: Response(png()))
    first = fetch.acquire(workspace, "Welcome Swallow")
    data = json.loads(path.read_text())
    data["notes"] = "Source revision changed"
    path.write_text(json.dumps(data))
    record(workspace, path)
    assert (workspace / first["path"]).is_file()
    with pytest.raises(ValueError, match="approval"):
        fetch.acquire(workspace, "Welcome Swallow")


@pytest.mark.parametrize(
    "url",
    [
        "http://archive.org/file.jpg",
        "https://localhost/file.jpg",
        "https://archive.org.evil.invalid/file.jpg",
        "https://user:secret@archive.org/file.jpg",
        "file:///etc/passwd",
    ],
)
def test_scan_urls_reject_unapproved_destinations(url):
    with pytest.raises(ValueError):
        sources.validate_url(url, scan=True)


def test_redirect_handler_checks_every_destination():
    import urllib.request

    handler = fetch.ProviderRedirect()
    request = urllib.request.Request("https://archive.org/approved")
    with pytest.raises(ValueError, match="provider"):
        handler.redirect_request(request, None, 302, "Found", {}, "https://localhost/secret")


def test_verified_temporary_scan_recovers_before_promotion(source_workspace, monkeypatch):
    workspace, path = source_workspace
    record(workspace, path)
    approve(workspace)
    monkeypatch.setattr(fetch, "open_remote", lambda _: Response(png()))
    promote = fetch.promote
    monkeypatch.setattr(
        fetch, "promote", lambda *args: (_ for _ in ()).throw(RuntimeError("crash"))
    )
    with pytest.raises(RuntimeError):
        fetch.acquire(workspace, "Welcome Swallow")
    assert list((workspace / "work/scans/.partial").glob("*.part"))
    monkeypatch.setattr(fetch, "promote", promote)
    monkeypatch.setattr(fetch, "open_remote", lambda _: pytest.fail("recovery must not refetch"))
    assert fetch.acquire(workspace, "Welcome Swallow")["recovered"] is True


def test_failed_attempt_can_retry_and_keeps_failure_event(source_workspace, monkeypatch):
    workspace, path = source_workspace
    record(workspace, path)
    approve(workspace)
    monkeypatch.setattr(fetch, "MAX_BYTES", 10)
    monkeypatch.setattr(fetch, "open_remote", lambda _: Response(png()))
    with pytest.raises(ValueError, match="Content-Length"):
        fetch.acquire(workspace, "Welcome Swallow")
    monkeypatch.setattr(fetch, "MAX_BYTES", 128 * 1024 * 1024)
    assert fetch.acquire(workspace, "Welcome Swallow")["changed"] is True
    with store.connect(workspace, readonly=True) as db:
        event = db.execute("SELECT notes FROM events WHERE action='fetch-failed'").fetchone()
        assert "Content-Length" in event[0]


def test_workspace_lock_excludes_other_mutations(source_workspace):
    from conftest import cli

    workspace = source_workspace[0]
    with store.mutation_lock(workspace):
        assert "lock" in cli(
            workspace, "inventory", "--checklist", str(workspace.parent / "au.json"), ok=False
        )
        assert "lock" in cli(workspace, "batch", "create", "locked", "Welcome Swallow", ok=False)


@pytest.mark.parametrize("limit", ["stream-bytes", "time"])
def test_limits_apply_without_content_length(source_workspace, monkeypatch, limit):
    workspace, path = source_workspace
    record(workspace, path)
    approve(workspace)

    def response(_):
        result = Response(png())
        del result.headers["Content-Length"]
        return result

    monkeypatch.setattr(fetch, "open_remote", response)
    if limit == "stream-bytes":
        monkeypatch.setattr(fetch, "MAX_BYTES", 10)
    else:
        times = iter([0, fetch.MAX_SECONDS + 1])
        monkeypatch.setattr(fetch.time, "monotonic", lambda: next(times))
    with pytest.raises(ValueError, match="limits"):
        fetch.acquire(workspace, "Welcome Swallow")
    with store.connect(workspace, readonly=True) as db:
        assert store.find_species(db, "Welcome Swallow")["state"] == "sourced"
        assert db.execute("SELECT COUNT(*) FROM downloads").fetchone()[0] == 0


def test_ku_gould_library_is_an_approved_scan_provider():
    # Upstream accepted KU Gould plates (brushturkey, honeyeaters); its masters are TIFF.
    url = "https://digital.lib.ku.edu/islandora/object/ku-gould%3A14918/datastream/OBJ/download"
    assert sources.validate_url(url, scan=True) == url
    with pytest.raises(ValueError):
        sources.validate_url("https://lib.ku.edu.evil.invalid/x.tif", scan=True)


def test_fetch_accepts_uncompressed_tiff_masters(source_workspace, monkeypatch):
    workspace, path = source_workspace
    record(workspace, path)
    approve(workspace)
    data = io.BytesIO()
    Image.new("RGB", (800, 1100), "white").save(data, "TIFF")
    monkeypatch.setattr(fetch, "open_remote", lambda _: Response(data.getvalue()))
    result = fetch.acquire(workspace, "Welcome Swallow")
    assert result["format"] == "TIFF" and result["path"].endswith(".tif")
