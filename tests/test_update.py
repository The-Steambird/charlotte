import dataclasses
import hashlib
import sys
import zipfile

from pathlib import Path
from types import SimpleNamespace

import pytest

import utils.update

from conftest import FakeReporter, forbid_call
from utils.errors import Cancelled, CharlotteError
from utils.update import (
    UpdateInfo,
    apply_update,
    check_for_update,
    clear_stale_files,
    install_bundle,
    parse_version,
    report_update,
    run_update,
    stream_to_file,
    zip_asset,
)
from utils.version import __version__


UPDATE_FIELDS = {"current", "latest", "available", "url", "notes", "download", "reason"}


def release(tag: str, url: str = "https://example/rel", body: str = "notes") -> dict:
    return {"tag_name": tag, "html_url": url, "body": body}


@pytest.mark.parametrize(
    "lower, higher",
    [("0.9.0", "0.10.0"), ("2.0", "2.0.1")],
)
def test_parse_version_ordering(lower, higher):
    assert parse_version(lower) < parse_version(higher)


def test_version_is_a_usable_tag():
    # A typo in pyproject.toml would otherwise only surface during an update check.
    assert parse_version(__version__)


def test_update_available(monkeypatch):
    asset = {
        "name": "charlotte-99.0.0.zip",
        "browser_download_url": "https://example/dl",
        "digest": "sha256:abc",
    }
    with_asset = release("v99.0.0") | {"assets": [asset]}
    monkeypatch.setattr(utils.update, "fetch_latest_release", lambda: with_asset)
    assert check_for_update() == UpdateInfo(
        current=__version__,
        latest="99.0.0",
        available=True,
        url="https://example/rel",
        notes="notes",
        download="https://example/dl",
        sha256="abc",
        reason=None,
    )


@pytest.mark.parametrize("tag", [f"v{__version__}", "v0.0.1"], ids=["same", "older"])
def test_not_available_when_current_or_ahead(monkeypatch, tag):
    monkeypatch.setattr(utils.update, "fetch_latest_release", lambda: release(tag))
    info = check_for_update()
    assert info.available is False
    assert info.reason is None


def unreachable():
    raise CharlotteError("HTTP 503")


@pytest.mark.parametrize(
    "fetch, reason",
    [
        (unreachable, "HTTP 503"),
        (lambda: release("nightly"), "unrecognized release tag"),
    ],
)
def test_declined_check_reports_reason(monkeypatch, fetch, reason):
    monkeypatch.setattr(utils.update, "fetch_latest_release", fetch)
    assert check_for_update() == UpdateInfo(current=__version__, reason=reason)


@pytest.mark.parametrize(
    "fetch", [lambda: release("v99.0.0"), unreachable], ids=["available", "failed"]
)
def test_event_shape_fixed_regardless_of_outcome(monkeypatch, reporter, fetch):
    monkeypatch.setattr(utils.update, "fetch_latest_release", fetch)
    report_update(reporter)
    assert len(reporter.events) == 1
    kind, data = reporter.events[0]
    assert kind == "update"
    assert set(data) == UPDATE_FIELDS


# --- self-apply ---


def test_zip_asset_picks_the_zip_only():
    assets = [{"name": "keys.json"}, {"name": "charlotte-cli.exe"}]
    assert zip_asset({"assets": assets}) == {}
    assets.append({"name": "charlotte-1.0.zip"})
    assert zip_asset({"assets": assets}) == {"name": "charlotte-1.0.zip"}


def bundle(path, **members: bytes):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def test_stream_to_file_returns_the_sha256(reporter, tmp_path):
    response = SimpleNamespace(headers={}, stream=lambda size: [b"ab", b"cd"])
    dest = tmp_path / "b.zip"

    assert stream_to_file(response, dest, reporter) == hashlib.sha256(b"abcd").hexdigest()
    assert dest.read_bytes() == b"abcd"


def test_stream_to_file_stops_on_cancel(reporter, monkeypatch, tmp_path):
    response = SimpleNamespace(headers={}, stream=lambda size: [b"ab", b"cd"])
    monkeypatch.setattr(reporter, "cancel_requested", lambda: True)

    with pytest.raises(Cancelled):
        stream_to_file(response, tmp_path / "b.zip", reporter)


def test_install_bundle_replaces_every_file_and_keeps_the_old_ones(tmp_path):
    (tmp_path / "charlotte-cli.exe").write_bytes(b"old cli")
    (tmp_path / "charlotte-gui.exe").write_bytes(b"old gui")
    members = {
        "charlotte/": b"",
        "charlotte/charlotte-cli.exe": b"new cli",
        "charlotte/charlotte-gui.exe": b"new gui",
    }

    install_bundle(bundle(tmp_path / "b.zip", **members), tmp_path)

    assert (tmp_path / "charlotte-cli.exe").read_bytes() == b"new cli"
    assert (tmp_path / "charlotte-gui.exe").read_bytes() == b"new gui"
    assert not (tmp_path / "charlotte").exists()
    assert (tmp_path / "charlotte-cli.exe.old").read_bytes() == b"old cli"
    assert (tmp_path / "charlotte-gui.exe.old").read_bytes() == b"old gui"


def test_install_bundle_rolls_back_when_a_write_fails(tmp_path):
    (tmp_path / "charlotte-cli.exe").write_bytes(b"old cli")
    (tmp_path / "blocked").write_bytes(b"")  # a file where the second member needs a folder
    members = {"charlotte/charlotte-cli.exe": b"new cli", "charlotte/blocked/x": b"x"}
    zip_path = bundle(tmp_path / "b.zip", **members)

    with pytest.raises(CharlotteError):
        install_bundle(zip_path, tmp_path)
    assert (tmp_path / "charlotte-cli.exe").read_bytes() == b"old cli"
    assert not (tmp_path / "charlotte-cli.exe.old").exists()


@pytest.mark.parametrize(
    "members", [{"charlotte/../escaped.exe": b"x"}, None], ids=["escapes", "not-a-zip"]
)
def test_install_bundle_rejects_a_bad_bundle_before_touching_anything(tmp_path, members):
    folder = tmp_path / "app"
    folder.mkdir()
    (folder / "charlotte-cli.exe").write_bytes(b"old cli")
    zip_path = tmp_path / "b.zip"
    if members is None:
        zip_path.write_bytes(b"<!doctype html>")
    else:
        bundle(zip_path, **members)

    with pytest.raises(CharlotteError):
        install_bundle(zip_path, folder)
    assert sorted(path.name for path in tmp_path.rglob("*")) == ["app", "b.zip", "charlotte-cli.exe"]


def released(tmp_path, monkeypatch, sha256: str | None = None) -> tuple[UpdateInfo, Path]:
    """A release whose download is a real bundle, checked against `sha256` or its real digest."""
    release_zip = bundle(tmp_path / "release.zip", **{"charlotte/charlotte-cli.exe": b"new"})
    zip_bytes = release_zip.read_bytes()
    actual = hashlib.sha256(zip_bytes).hexdigest()

    def fake_download(url, dest, reporter):
        dest.write_bytes(zip_bytes)
        return actual

    folder = tmp_path / "app"
    folder.mkdir()
    monkeypatch.setattr(utils.update, "download_bundle", fake_download)
    monkeypatch.setattr(utils.update, "running_exe", lambda: folder / "charlotte-cli.exe")
    info = UpdateInfo(current=__version__, available=True, download="u", sha256=sha256 or actual)
    return info, folder


def test_apply_update_installs_a_download_matching_its_checksum(reporter, monkeypatch, tmp_path):
    info, folder = released(tmp_path, monkeypatch)

    apply_update(info, reporter)
    assert (folder / "charlotte-cli.exe").read_bytes() == b"new"
    assert not (folder / "charlotte-update.zip").exists()


def test_apply_update_refuses_a_download_that_does_not_match(reporter, monkeypatch, tmp_path):
    info, folder = released(tmp_path, monkeypatch, sha256="0" * 64)

    with pytest.raises(CharlotteError):
        apply_update(info, reporter)
    assert list(folder.iterdir()) == []


def test_apply_update_refuses_a_release_without_a_checksum(reporter, monkeypatch, tmp_path):
    info, _ = released(tmp_path, monkeypatch)
    monkeypatch.setattr(utils.update, "download_bundle", forbid_call)

    with pytest.raises(CharlotteError):
        apply_update(dataclasses.replace(info, sha256=None), reporter)


def test_clear_stale_files_removes_every_old_file(monkeypatch, tmp_path):
    for name in ("charlotte-cli.exe", "charlotte-cli.exe.old", "charlotte-gui.exe.old"):
        (tmp_path / name).write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(utils.update, "running_exe", lambda: tmp_path / "charlotte-cli.exe")

    clear_stale_files()
    assert [path.name for path in tmp_path.iterdir()] == ["charlotte-cli.exe"]


# --- run_update ---


def update_available(monkeypatch, frozen: bool = True) -> None:
    monkeypatch.setattr(utils.update, "fetch_latest_release", lambda: release("v99.0.0"))
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)


def test_run_update_from_source_only_reports(monkeypatch, reporter):
    update_available(monkeypatch, frozen=False)
    monkeypatch.setattr(utils.update, "apply_update", forbid_call)

    run_update(reporter, json_mode=False)
    assert reporter.prompts == []
    assert [kind for kind, _ in reporter.events] == ["update"]


def test_run_update_no_prompt_when_up_to_date(monkeypatch, reporter):
    monkeypatch.setattr(utils.update, "fetch_latest_release", lambda: release(f"v{__version__}"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(utils.update, "apply_update", forbid_call)

    run_update(reporter, json_mode=False)
    assert reporter.prompts == []


def test_run_update_decline_skips_install(monkeypatch, reporter):
    update_available(monkeypatch)
    monkeypatch.setattr(utils.update, "apply_update", forbid_call)

    run_update(reporter, json_mode=False)  # FakeReporter answers False by default
    assert len(reporter.prompts) == 1


@pytest.mark.parametrize("json_mode", [False, True], ids=["console", "gui"])
def test_run_update_installs_on_yes(monkeypatch, json_mode):
    update_available(monkeypatch)
    applied = []
    paused = []
    monkeypatch.setattr(utils.update, "apply_update", lambda info, reporter: applied.append(info))
    monkeypatch.setattr(utils.update, "pause_before_exit", lambda: paused.append(True))
    reporter = FakeReporter(answer=True)

    run_update(reporter, json_mode)
    assert [info.latest for info in applied] == ["99.0.0"]
    assert ("update_install", {"installed": True, "reason": None}) in reporter.events
    assert paused == ([] if json_mode else [True])  # only a console window needs holding open


def fail_with(error):
    def apply(info, reporter):
        raise error

    return apply


@pytest.mark.parametrize(
    "error, reason",
    [(CharlotteError("locked"), "locked"), (Cancelled(), "The update was cancelled.")],
    ids=["failed", "cancelled"],
)
def test_run_update_reports_why_an_install_did_not_happen(monkeypatch, error, reason):
    update_available(monkeypatch)
    monkeypatch.setattr(utils.update, "apply_update", fail_with(error))
    monkeypatch.setattr(utils.update, "pause_before_exit", forbid_call)
    reporter = FakeReporter(answer=True)

    run_update(reporter, json_mode=False)
    assert ("update_install", {"installed": False, "reason": reason}) in reporter.events
