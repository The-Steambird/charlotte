import io
import zipfile

import pytest

import resources.subtitles

from resources.subtitles import local_subtitle_path, stored_commit, sync_subtitles, write_commit
from utils.errors import CharlotteError


# Top-level directory GitLab puts in the subpath archive; sync_subtitles strips it.
ARCHIVE_ROOT = "animegamedata2-main-Subtitle"


def make_archive(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    buf.seek(0)
    return zipfile.ZipFile(buf)


def stub_upstream(monkeypatch, entries, commit="abc123"):
    monkeypatch.setattr(resources.subtitles, "latest_commit", lambda: commit)
    monkeypatch.setattr(resources.subtitles, "fetch_archive", lambda: make_archive(entries))


def forbid_fetch():
    pytest.fail("unexpected archive download")


# --- helpers and the sync marker ---


def test_local_subtitle_path(tmp_app_root):
    expected = tmp_app_root / "Subtitle" / "EN" / "Cs_X_EN.srt"
    assert local_subtitle_path("Cs_X", "EN") == expected


def test_commit_marker_roundtrip(tmp_app_root):
    assert stored_commit() == ""
    (tmp_app_root / "Subtitle").mkdir()
    write_commit("abc123")
    assert stored_commit() == "abc123"


def test_stored_commit_tolerates_corrupt_marker(tmp_app_root):
    marker = tmp_app_root / "Subtitle" / ".sync.json"
    marker.parent.mkdir()
    marker.write_bytes(b"not json")
    assert stored_commit() == ""
    marker.write_bytes(b"[1, 2]")
    assert stored_commit() == ""


# --- sync_subtitles ---


def test_sync_writes_files_and_marker(tmp_app_root, reporter, monkeypatch):
    entries = {
        f"{ARCHIVE_ROOT}/Subtitle/EN/Cs_A_EN.srt": b"english",
        f"{ARCHIVE_ROOT}/Subtitle/JP/Cs_A_JP.srt": b"japanese",
    }
    stub_upstream(monkeypatch, entries)

    sync_subtitles(reporter)

    assert local_subtitle_path("Cs_A", "EN").read_bytes() == b"english"
    assert local_subtitle_path("Cs_A", "JP").read_bytes() == b"japanese"
    assert stored_commit() == "abc123"


def test_sync_overwrites_stale_local_file(tmp_app_root, reporter, monkeypatch):
    stale = local_subtitle_path("Cs_A", "EN")
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")
    stub_upstream(monkeypatch, {f"{ARCHIVE_ROOT}/Subtitle/EN/Cs_A_EN.srt": b"new"})

    sync_subtitles(reporter)

    assert stale.read_bytes() == b"new"


def test_sync_skips_when_up_to_date(tmp_app_root, reporter, monkeypatch):
    (tmp_app_root / "Subtitle").mkdir()
    write_commit("abc123")
    monkeypatch.setattr(resources.subtitles, "latest_commit", lambda: "abc123")
    monkeypatch.setattr(resources.subtitles, "fetch_archive", forbid_fetch)

    sync_subtitles(reporter)

    # forbid_fetch fails the test if a download was attempted; the marker stays put.
    assert stored_commit() == "abc123"


def test_sync_filters_archive_entries(tmp_app_root, reporter, monkeypatch):
    entries = {
        f"{ARCHIVE_ROOT}/Subtitle/EN/Cs_A_EN.srt": b"kept",
        f"{ARCHIVE_ROOT}/Subtitle/EN/notes.txt": b"not a subtitle",
        f"{ARCHIVE_ROOT}/README.md": b"not under Subtitle/",
        f"{ARCHIVE_ROOT}/Subtitle/../evil.srt": b"zip-slip attempt",
    }
    stub_upstream(monkeypatch, entries)

    sync_subtitles(reporter)

    assert local_subtitle_path("Cs_A", "EN").read_bytes() == b"kept"
    assert not (tmp_app_root / "evil.srt").exists()
    assert not (tmp_app_root / "Subtitle" / "EN" / "notes.txt").exists()
    # All valid targets landed, so the marker is still written.
    assert stored_commit() == "abc123"


def test_sync_archive_without_subtitles_keeps_cache(tmp_app_root, reporter, monkeypatch):
    stub_upstream(monkeypatch, {f"{ARCHIVE_ROOT}/README.md": b"no srt files"})
    sync_subtitles(reporter)
    assert stored_commit() == ""


def test_sync_network_failure_falls_back(reporter, monkeypatch):
    def down():
        raise CharlotteError("net down")

    monkeypatch.setattr(resources.subtitles, "latest_commit", down)
    monkeypatch.setattr(resources.subtitles, "fetch_archive", forbid_fetch)
    # The contract is falling back silently: no exception, no download attempt.
    sync_subtitles(reporter)


def test_sync_download_failure_falls_back(tmp_app_root, reporter, monkeypatch):
    def down():
        raise CharlotteError("download failed")

    monkeypatch.setattr(resources.subtitles, "latest_commit", lambda: "abc123")
    monkeypatch.setattr(resources.subtitles, "fetch_archive", down)

    sync_subtitles(reporter)

    assert stored_commit() == ""


def test_sync_partial_write_skips_marker(tmp_app_root, reporter, monkeypatch):
    entries = {
        f"{ARCHIVE_ROOT}/Subtitle/EN/Cs_A_EN.srt": b"ok",
        f"{ARCHIVE_ROOT}/Subtitle/EN/Cs_B_EN.srt": b"fails to write",
    }
    stub_upstream(monkeypatch, entries)
    real_extract = resources.subtitles.extract_member

    def flaky_extract(archive, name, target):
        if name.endswith("Cs_B_EN.srt"):
            return False
        return real_extract(archive, name, target)

    monkeypatch.setattr(resources.subtitles, "extract_member", flaky_extract)

    sync_subtitles(reporter)

    assert local_subtitle_path("Cs_A", "EN").read_bytes() == b"ok"
    # A partial sync must not record the commit, so the next run retries the download.
    assert stored_commit() == ""
