import sys
import zipfile

import pytest

import utils.update

from conftest import FakeReporter, forbid_call
from utils.errors import CharlotteError
from utils.update import (
    UpdateInfo,
    apply_update,
    asset_download_url,
    check_for_update,
    clear_stale_binary,
    extract_binary,
    parse_version,
    report_update,
    run_update,
    swap_binary,
)
from utils.version import __version__


UPDATE_FIELDS = {"current", "latest", "available", "url", "notes", "download", "reason"}


def release(tag: str, url: str = "https://example/rel", body: str = "notes") -> dict:
    return {"tag_name": tag, "html_url": url, "body": body}


def test_parse_version_ignores_leading_v():
    assert parse_version("v1.2.3") == parse_version("1.2.3")


@pytest.mark.parametrize(
    "lower, higher",
    [
        ("0.9.0", "0.10.0"),
        ("2.0", "2.0.1"),  # a flat key would compare 2.0's phase rank against 2.0.1's patch digit
        ("1.2.3a1", "1.2.3b1"),
        ("1.2.3b1", "1.2.3b2"),
        ("1.2.3b2", "1.2.3rc1"),
        ("1.2.3rc1", "1.2.3"),
        ("1.2.9", "1.3.0b1"),
    ],
)
def test_parse_version_ordering(lower, higher):
    assert parse_version(lower) < parse_version(higher)


def test_version_is_a_usable_tag():
    # A typo in pyproject.toml would otherwise only surface during an update check.
    assert parse_version(__version__)


def test_update_available(monkeypatch):
    with_asset = release("v99.0.0") | {
        "assets": [{"name": "charlotte-99.0.0.zip", "browser_download_url": "https://example/dl"}]
    }
    monkeypatch.setattr(utils.update, "fetch_latest_release", lambda: with_asset)
    assert check_for_update() == UpdateInfo(
        current=__version__,
        latest="99.0.0",
        available=True,
        url="https://example/rel",
        notes="notes",
        download="https://example/dl",
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
        (lambda: {"html_url": "x"}, "no release tag found"),
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


def test_asset_download_url_picks_the_zip_only():
    assets = [
        {"name": "keys.json", "browser_download_url": "u1"},
        {"name": "charlotte-cli.exe", "browser_download_url": "u2"},
    ]
    assert asset_download_url({"assets": assets}) is None
    assert asset_download_url({}) is None
    assets.append({"name": "charlotte-1.0.zip", "browser_download_url": "u3"})
    assert asset_download_url({"assets": assets}) == "u3"


def test_apply_update_declines_without_asset(reporter, monkeypatch, tmp_path):
    # running_exe is stubbed because the cleanup unlinks beside it.
    monkeypatch.setattr(utils.update, "running_exe", lambda: tmp_path / "charlotte-cli.exe")
    info = UpdateInfo(current=__version__, latest="99.0.0", available=True)
    assert apply_update(info, reporter) is False


def bundle(path, **members: bytes):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def test_extract_binary_strips_wrapping_folder(tmp_path):
    zip_path = bundle(tmp_path / "b.zip", **{"charlotte-1.0/charlotte-cli.exe": b"MZengine"})
    dest = tmp_path / "charlotte-cli.exe.new"
    extract_binary(zip_path, dest)
    assert dest.read_bytes() == b"MZengine"


def test_extract_binary_rejects_zip_without_engine(tmp_path):
    zip_path = bundle(tmp_path / "b.zip", **{"charlotte-gui.exe": b"MZgui"})
    with pytest.raises(CharlotteError):
        extract_binary(zip_path, tmp_path / "charlotte-cli.exe.new")


def test_extract_binary_rejects_non_zip(tmp_path):
    not_zip = tmp_path / "b.zip"
    not_zip.write_bytes(b"<!doctype html>")
    with pytest.raises(CharlotteError):
        extract_binary(not_zip, tmp_path / "charlotte-cli.exe.new")


def test_apply_update_cleans_up_bundle_and_partial(reporter, monkeypatch, tmp_path):
    exe = tmp_path / "charlotte-cli.exe"
    exe.write_bytes(b"MZold")
    monkeypatch.setattr(utils.update, "running_exe", lambda: exe)

    def fake_download(url, dest, reporter):
        bundle(dest, **{"charlotte-cli.exe": b"not a binary"})

    monkeypatch.setattr(utils.update, "download_bundle", fake_download)
    info = UpdateInfo(current=__version__, latest="99.0.0", available=True, download="u")
    assert apply_update(info, reporter) is False
    assert exe.read_bytes() == b"MZold"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["charlotte-cli.exe"]


def test_apply_update_swaps_from_bundle(reporter, monkeypatch, tmp_path):
    exe = tmp_path / "charlotte-cli.exe"
    exe.write_bytes(b"MZold")
    monkeypatch.setattr(utils.update, "running_exe", lambda: exe)

    def fake_download(url, dest, reporter):
        bundle(dest, **{"charlotte-gui.exe": b"MZgui", "charlotte-cli.exe": b"MZnew"})

    monkeypatch.setattr(utils.update, "download_bundle", fake_download)
    info = UpdateInfo(current=__version__, latest="99.0.0", available=True, download="u")
    assert apply_update(info, reporter) is True
    assert exe.read_bytes() == b"MZnew"
    assert (tmp_path / "charlotte-cli.exe.old").read_bytes() == b"MZold"
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "charlotte-cli.exe",
        "charlotte-cli.exe.old",
    ]


def test_swap_binary_rolls_back_when_new_missing(monkeypatch, tmp_path):
    exe = tmp_path / "charlotte-cli.exe"
    exe.write_bytes(b"OLD")
    missing_new = tmp_path / "charlotte-cli.exe.new"  # never created, which makes the rename raise
    monkeypatch.setattr(utils.update, "running_exe", lambda: exe)

    with pytest.raises(CharlotteError):
        swap_binary(missing_new)
    assert exe.read_bytes() == b"OLD"


def test_clear_stale_binary_removes_old(monkeypatch, tmp_path):
    exe = tmp_path / "charlotte-cli.exe"
    exe.write_bytes(b"NEW")
    old = tmp_path / "charlotte-cli.exe.old"
    old.write_bytes(b"OLD")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(utils.update, "running_exe", lambda: exe)

    clear_stale_binary()
    assert not old.exists()
    assert exe.exists()


# --- run_update ---


def update_available(monkeypatch, frozen: bool = True) -> None:
    monkeypatch.setattr(utils.update, "fetch_latest_release", lambda: release("v99.0.0"))
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)


@pytest.mark.parametrize(
    "frozen, json_mode",
    [
        (False, False),  # from source there is no exe to swap
        (True, True),  # under --json the GUI owns installing
    ],
)
def test_run_update_report_only_when_not_standalone(monkeypatch, reporter, frozen, json_mode):
    update_available(monkeypatch, frozen=frozen)
    monkeypatch.setattr(utils.update, "apply_update", forbid_call)

    run_update(reporter, json_mode)
    assert reporter.prompts == []
    assert reporter.events[0][0] == "update"


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


def test_run_update_installs_on_yes(monkeypatch):
    update_available(monkeypatch)
    applied = []

    def fake_apply(info, reporter):
        applied.append(info)
        return True

    paused = []
    monkeypatch.setattr(utils.update, "apply_update", fake_apply)
    monkeypatch.setattr(utils.update, "pause_before_exit", lambda: paused.append(True))

    run_update(FakeReporter(answer=True), json_mode=False)
    assert [info.latest for info in applied] == ["99.0.0"]
    assert paused  # holds the console open so the result is readable


def test_run_update_failed_install_skips_pause(monkeypatch):
    update_available(monkeypatch)
    monkeypatch.setattr(utils.update, "apply_update", lambda info, reporter: False)
    monkeypatch.setattr(utils.update, "pause_before_exit", forbid_call)

    run_update(FakeReporter(answer=True), json_mode=False)
