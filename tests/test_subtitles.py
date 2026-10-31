import io
import zipfile

import orjson
import pytest

import resources.subtitles

from resources.subtitles import (
    gitlab_commit,
    stored_commit,
    subtitle_path,
    sync_subtitles,
    write_commit,
)
from utils.errors import CharlotteError


# GitLab wraps the subpath archive in this top-level directory, and sync_subtitles strips it.
ARCHIVE_ROOT = "animegamedata2-main-Subtitle"
GITLAB_COMMITS = "https://gitlab.com/api/"
GITLAB_ARCHIVE = "https://gitlab.com/Dimbreath/"
GITHUB_COMMITS = "https://api.github.com/repos/DimbreathBot/AnimeGameData/commits"
GITHUB_TREE = "https://api.github.com/repos/DimbreathBot/AnimeGameData/git/trees/"
GITHUB_RAW = "https://raw.githubusercontent.com/DimbreathBot/AnimeGameData/abc123/"


def make_archive(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buf.getvalue()


def serve(monkeypatch, responses):
    """Answers each request with the response whose key its URL starts with, and fails the rest
    like an unreachable host. Returns the URLs asked for."""
    requested = []

    def fetch(http, url, timeout=10.0):
        requested.append(url)
        for prefix, body in responses.items():
            if url.startswith(prefix):
                return body
        raise CharlotteError("unreachable")

    monkeypatch.setattr(resources.subtitles, "fetch", fetch)
    return requested


def serve_gitlab(monkeypatch, entries):
    return serve(
        monkeypatch,
        {GITLAB_COMMITS: b'[{"id": "abc123"}]', GITLAB_ARCHIVE: make_archive(entries)},
    )


# --- the sync marker ---


def test_stored_commit_tolerates_corrupt_marker(tmp_app_root):
    marker = tmp_app_root / "Subtitle" / ".sync.json"
    marker.parent.mkdir()
    marker.write_bytes(b"not json")
    assert stored_commit() == ""


def test_empty_commit_list_is_an_error_not_a_crash(monkeypatch):
    """GitLab answers 200 with [] for a path with no history and for a missing branch, and an
    IndexError would crash every run at startup."""
    serve(monkeypatch, {GITLAB_COMMITS: b"[]"})
    with pytest.raises(CharlotteError):
        gitlab_commit(None)


# --- sync from GitLab ---


def test_sync_writes_files_and_marker(tmp_app_root, reporter, monkeypatch):
    stale = subtitle_path("Cs_A", "EN")
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")
    serve_gitlab(
        monkeypatch,
        {
            f"{ARCHIVE_ROOT}/Subtitle/EN/Cs_A_EN.srt": b"english",
            f"{ARCHIVE_ROOT}/Subtitle/JP/Cs_A_JP.srt": b"japanese",
        },
    )

    sync_subtitles(reporter)

    assert stale.read_bytes() == b"english"
    assert subtitle_path("Cs_A", "JP").read_bytes() == b"japanese"
    assert stored_commit() == "abc123"


def test_sync_skips_when_up_to_date(tmp_app_root, reporter, monkeypatch):
    (tmp_app_root / "Subtitle").mkdir()
    write_commit("abc123")
    requested = serve(monkeypatch, {GITLAB_COMMITS: b'[{"id": "abc123"}]'})

    sync_subtitles(reporter)

    assert len(requested) == 1
    assert requested[0].startswith(GITLAB_COMMITS)


def test_sync_filters_archive_entries(tmp_app_root, reporter, monkeypatch):
    serve_gitlab(
        monkeypatch,
        {
            f"{ARCHIVE_ROOT}/Subtitle/EN/Cs_A_EN.srt": b"kept",
            f"{ARCHIVE_ROOT}/Subtitle/EN/notes.txt": b"not a subtitle",
            f"{ARCHIVE_ROOT}/README.md": b"not under Subtitle/",
            f"{ARCHIVE_ROOT}/Subtitle/../evil.srt": b"zip-slip attempt",
        },
    )

    sync_subtitles(reporter)

    assert subtitle_path("Cs_A", "EN").read_bytes() == b"kept"
    assert not (tmp_app_root / "evil.srt").exists()
    assert not (tmp_app_root / "Subtitle" / "EN" / "notes.txt").exists()
    assert stored_commit() == "abc123"


def test_sync_archive_without_subtitles_keeps_cache(tmp_app_root, reporter, monkeypatch):
    serve_gitlab(monkeypatch, {f"{ARCHIVE_ROOT}/README.md": b"no srt files"})
    sync_subtitles(reporter)
    assert stored_commit() == ""


def test_sync_write_failure_skips_marker(tmp_app_root, reporter, monkeypatch):
    serve_gitlab(
        monkeypatch,
        {
            f"{ARCHIVE_ROOT}/Subtitle/EN/Cs_A_EN.srt": b"ok",
            f"{ARCHIVE_ROOT}/Subtitle/EN/Cs_B_EN.srt": b"fails to write",
        },
    )
    subtitle_path("Cs_B", "EN").mkdir(parents=True)

    sync_subtitles(reporter)

    assert stored_commit() == ""  # the next run retries


# --- the GitHub mirror ---


def mirror_tree(*entries):
    tree = [{"path": "EN", "type": "tree", "sha": "0" * 40}]
    tree += [{"path": path, "type": "blob", "sha": sha} for path, sha in entries]
    return orjson.dumps({"tree": tree})


def test_gitlab_failure_falls_back_to_the_mirror_for_changed_files(
    tmp_app_root, reporter, monkeypatch
):
    current = subtitle_path("Cs_A", "EN")
    current.parent.mkdir(parents=True)
    current.write_bytes(b"same")
    requested = serve(
        monkeypatch,
        {
            GITHUB_COMMITS: b'[{"sha": "abc123"}]',
            GITHUB_TREE: mirror_tree(
                # The ID git hash-object gives b"same", the cached copy's content.
                ("EN/Cs_A_EN.srt", "5a66c000ab56fb7018b5ec24cdb24c94c00e2142"),
                ("EN/Cs_B_EN.srt", "1" * 40),
            ),
            GITHUB_RAW: b"new",
        },
    )

    sync_subtitles(reporter)

    downloaded = [url for url in requested if url.startswith(GITHUB_RAW)]
    assert downloaded == [f"{GITHUB_RAW}Subtitle/EN/Cs_B_EN.srt"]
    assert current.read_bytes() == b"same"
    assert subtitle_path("Cs_B", "EN").read_bytes() == b"new"
    assert stored_commit() == "abc123"


def test_cancel_during_a_mirror_download_stops_without_raising(tmp_app_root, reporter, monkeypatch):
    """A cold cache takes about a minute from the mirror. Cancelled raised here would escape
    main.py, and the batch's first checkpoint reports the cancel instead."""
    serve(
        monkeypatch,
        {
            GITHUB_COMMITS: b'[{"sha": "abc123"}]',
            GITHUB_TREE: mirror_tree(("EN/Cs_B_EN.srt", "1" * 40)),
            GITHUB_RAW: b"new",
        },
    )
    monkeypatch.setattr(reporter, "cancel_requested", lambda: True)

    sync_subtitles(reporter)

    assert not subtitle_path("Cs_B", "EN").exists()
    assert stored_commit() == ""


def test_a_failed_mirror_sync_resumes_where_it_stopped(tmp_app_root, reporter, monkeypatch):
    new_blob = "3e5126c4e761fd09582fc517918a1601b218dff0"
    file_a = f"{GITHUB_RAW}Subtitle/EN/Cs_A_EN.srt"
    file_b = f"{GITHUB_RAW}Subtitle/EN/Cs_B_EN.srt"
    responses = {
        GITHUB_COMMITS: b'[{"sha": "abc123"}]',
        GITHUB_TREE: mirror_tree(("EN/Cs_A_EN.srt", new_blob), ("EN/Cs_B_EN.srt", new_blob)),
        file_a: b"new",
    }
    serve(monkeypatch, responses)

    sync_subtitles(reporter)

    assert subtitle_path("Cs_A", "EN").read_bytes() == b"new"
    assert stored_commit() == ""

    requested = serve(monkeypatch, responses | {file_b: b"new"})
    sync_subtitles(reporter)

    assert [url for url in requested if url.startswith(GITHUB_RAW)] == [file_b]
    assert stored_commit() == "abc123"


@pytest.mark.parametrize(
    "mirror",
    [
        {},
        {
            GITHUB_COMMITS: b'[{"sha": "abc123"}]',
            GITHUB_TREE: mirror_tree(("EN/Cs_B_EN.srt", "1" * 40)),
        },
    ],
    ids=["mirror-down", "download-fails"],
)
def test_both_sources_failing_keeps_the_cache(tmp_app_root, reporter, monkeypatch, mirror):
    serve(monkeypatch, {GITLAB_COMMITS: b'[{"id": "abc123"}]', **mirror})
    sync_subtitles(reporter)
    assert stored_commit() == ""
