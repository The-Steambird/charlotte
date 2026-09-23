from pathlib import Path
from types import SimpleNamespace

import orjson
import pytest

import pipeline
import resources.keys

from conftest import FakeReporter, chunk, flag_value, forbid_call, input_files
from pipeline import (
    Options,
    crack_all,
    crack_usm,
    probe_usm,
    process_subtitles,
    process_usm,
)
from resources.keys import Keys, calculate_key_from_filename
from resources.subtitles import local_subtitle_path
from stages.crack import Recovery
from utils.errors import Cancelled, CharlotteError, Skipped


KEYS_DATA = {"list": [{"version": "5.3", "videoKey": 111, "videos": ["Cs_A"]}]}
SRT = "1\n00:00:01,000 --> 00:00:02,000\nHi\n"


def write_subtitle(stem, lang, text=SRT):
    path = local_subtitle_path(stem, lang)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_keys(reporter, monkeypatch, data=None, upstream=None):
    """A real Keys over `data` on disk, reaching a fake upstream that serves `upstream`."""
    if data is not None:
        (resources.keys.app_root() / "keys.json").write_bytes(orjson.dumps(data))
    fetch = (lambda: orjson.dumps(upstream)) if upstream is not None else forbid_call
    monkeypatch.setattr(resources.keys, "fetch_upstream_keys", fetch)
    return Keys(reporter)


def last_event(reporter, kind):
    last_kind, data = reporter.events[-1]
    assert last_kind == kind
    return data


def make_options(tmp_path, **overrides) -> Options:
    (tmp_path / "out").mkdir(exist_ok=True)
    defaults = {
        "output": str(tmp_path / "out"),
        "no_cleanup": False,
        "vapoursynth": False,
        "crf": 0.0,
        "preset": "fast",
        "x265_params": None,
    }
    return Options(**defaults | overrides)


def make_run(tmp_path, chunks=None, **overrides):
    usm_file = tmp_path / "Cs_Test.usm"
    usm_file.write_bytes(chunks or chunk(b"@SFV", b"video") + chunk(b"@SFA", b"audio"))
    keys = SimpleNamespace(decryption_key=lambda stem: (bytes(4), bytes(4)))
    return usm_file, make_options(tmp_path, **overrides), keys


# --- probe ---


def test_probe_reports_available(tmp_app_root, reporter, monkeypatch):
    monkeypatch.setattr(pipeline, "find_vs_script", lambda stem: "Cs_A")
    write_subtitle("Cs_A", "EN")
    write_subtitle("Cs_A", "JP")

    probe_usm(tmp_app_root / "Cs_A.usm", make_keys(reporter, monkeypatch, KEYS_DATA), reporter)

    assert last_event(reporter, "probe") == {
        "file": "Cs_A.usm",
        "stem": "Cs_A",
        "key": True,
        "version": "5.3",
        "subtitles": ["EN", "JP"],
        "vs_script": "Cs_A",
    }


def test_probe_reports_missing_when_upstream_has_nothing(tmp_app_root, reporter, monkeypatch):
    monkeypatch.setattr(pipeline, "find_vs_script", lambda stem: None)
    keys = make_keys(reporter, monkeypatch, {"list": []}, upstream={"list": []})

    probe_usm(tmp_app_root / "Cs_A.usm", keys, reporter)

    data = last_event(reporter, "probe")
    assert data["key"] is False
    assert data["version"] is None
    assert data["subtitles"] == []
    assert data["vs_script"] is None
    assert reporter.prompts == []


def test_probe_picks_up_an_accepted_upstream_update(tmp_app_root, reporter, monkeypatch):
    """The whole point of probing through Keys: a stem the local file misses is reported with
    its key and version once the update is accepted, without a second probe run."""
    monkeypatch.setattr(pipeline, "find_vs_script", lambda stem: None)
    reporter.answer = True
    keys = make_keys(reporter, monkeypatch, {"list": []}, upstream=KEYS_DATA)

    probe_usm(tmp_app_root / "Cs_A.usm", keys, reporter)

    data = last_event(reporter, "probe")
    assert data["key"] is True
    assert data["version"] == "5.3"
    assert len(reporter.prompts) == 1


def test_probe_reports_missing_when_the_update_is_declined(tmp_app_root, reporter, monkeypatch):
    monkeypatch.setattr(pipeline, "find_vs_script", lambda stem: None)
    reporter.answer = False
    keys = make_keys(reporter, monkeypatch, {"list": []}, upstream=KEYS_DATA)

    probe_usm(tmp_app_root / "Cs_A.usm", keys, reporter)

    data = last_event(reporter, "probe")
    assert data["key"] is False
    assert data["version"] is None


def test_probe_remaps_subtitle_stem_only(tmp_app_root, reporter, monkeypatch):
    """BASENAME_FIXES applies to the subtitle lookup; the key and VapourSynth script
    keep the original stem."""
    seen_vs_stems = []
    monkeypatch.setattr(pipeline, "find_vs_script", seen_vs_stems.append)  # returns None
    write_subtitle("Cs_DQAQ200211_WanYeXianVideo", "EN")

    keys = make_keys(reporter, monkeypatch, {"list": []}, upstream={"list": []})
    probe_usm(tmp_app_root / "Cs_200211_WanYeXianVideo.usm", keys, reporter)

    data = last_event(reporter, "probe")
    assert data["stem"] == "Cs_200211_WanYeXianVideo"
    assert data["subtitles"] == ["EN"]
    assert seen_vs_stems == ["Cs_200211_WanYeXianVideo"]


# --- cancel and key fallback ---


class StopDuringDemux(FakeReporter):
    """Stays quiet through process_usm's two pre-demux checkpoints and raises at the first
    one inside the demux loop."""

    def __init__(self, error):
        super().__init__()
        self.error = error
        self.checks = 0

    def checkpoint(self):
        self.checks += 1
        if self.checks > 2:
            raise self.error


@pytest.mark.parametrize("error", [Cancelled, Skipped])
@pytest.mark.parametrize("no_cleanup", [False, True])
def test_stop_mid_demux_cleans_partial_files_unless_nc(tmp_path, error, no_cleanup):
    past_checkpoint = b"".join(chunk(b"@SFA", b"x") for _ in range(150))
    usm_file, opts, keys = make_run(tmp_path, chunks=past_checkpoint, no_cleanup=no_cleanup)

    with pytest.raises(error):
        process_usm(usm_file, opts, StopDuringDemux(error), keys)

    assert (tmp_path / "out" / "Cs_Test" / "Cs_Test_0.hca").exists() is no_cleanup


def test_missing_key_falls_back_to_cracking(tmp_path, reporter, monkeypatch):
    cracked = []

    def crack(usm_file, reporter):
        cracked.append(usm_file)
        return Recovery(None, "no IVF video stream in this file")

    monkeypatch.setattr(pipeline, "crack_key", crack)
    monkeypatch.setattr(resources.keys, "fetch_upstream_keys", lambda: None)
    usm_file, opts, _ = make_run(tmp_path)

    process_usm(usm_file, opts, reporter, Keys(reporter))  # a real Keys that misses everywhere

    assert cracked == [usm_file]
    assert ("job_skipped", {"file": "Cs_Test.usm", "reason": "no_key"}) in reporter.events


def test_stream_cipher_is_skipped_even_with_a_key(tmp_path, reporter):
    header = chunk(b"@SFV", b"@UTF\x00VIDEO_HDRINFO\x00width\x00nonce\x00\x00", data_type=1)
    usm_file, opts, keys = make_run(tmp_path, chunks=header + chunk(b"@SFV", b"video"))

    process_usm(usm_file, opts, reporter, keys)

    assert last_event(reporter, "job_skipped") == {"file": "Cs_Test.usm", "reason": "unsupported"}
    assert not (tmp_path / "out" / "Cs_Test").exists()


# --- full run: output layout and cleanup ---


@pytest.fixture
def stub_stages(monkeypatch):
    """Everything after demux is stubbed, leaving one audio track and EN plus JP subtitles
    on disk. The encode writes its .part whether or not it reports `ok`, the way a failing
    ffmpeg does."""
    calls = SimpleNamespace(ok=True, encode=None, mux=None, mux_error=None)

    def audio(hca_files, audio_files, *args, **kwargs):
        for path in audio_files:
            path.write_bytes(b"flac")

    def subtitles(stem, output_path):
        (output_path / "subs").mkdir()
        files = [output_path / "subs" / f"{stem}_{lang}.ass" for lang in ("EN", "JP")]
        for path in files:
            path.write_bytes(b"ass")
        return files

    def encode(source, reporter, ffmpeg_args, **kwargs):
        calls.encode = kwargs | {"source": source, "ffmpeg_args": ffmpeg_args}
        Path(ffmpeg_args[-1]).write_bytes(b"hevc" if calls.ok else b"trunc")
        return calls.ok

    def mux(video, output_file, *args, **kwargs):
        calls.mux = kwargs
        output_file.write_bytes(b"mkv")
        if calls.mux_error:
            raise calls.mux_error

    monkeypatch.setattr(pipeline, "process_audio", audio)
    monkeypatch.setattr(pipeline, "process_subtitles", subtitles)
    monkeypatch.setattr(pipeline, "vapoursynth_filter", encode)
    monkeypatch.setattr(pipeline, "mux", mux)
    return calls


def test_run_writes_mkv_and_clears_intermediates(stub_stages, tmp_path, reporter):
    usm_file, opts, keys = make_run(tmp_path)

    process_usm(usm_file, opts, reporter, keys)

    work_dir = tmp_path / "out" / "Cs_Test"
    assert (work_dir / "Cs_Test.mkv").is_file()
    assert list(work_dir.glob("*.ivf")) == []
    assert list(work_dir.glob("*.hca")) == []
    assert ("result", {
        "file": "Cs_Test.usm",
        "stem": "Cs_Test",
        "output": str(work_dir / "Cs_Test.mkv"),
        "status": "ok",
    }) in reporter.events  # fmt: skip


def test_failure_leaves_no_mkv_and_clears_intermediates(stub_stages, tmp_path, reporter):
    """A mux that dies mid-write leaves a truncated file behind, and it must not land where
    --skip-existing would take it for a finished one."""
    stub_stages.mux_error = CharlotteError("Muxing failed")
    usm_file, opts, keys = make_run(tmp_path)

    with pytest.raises(CharlotteError):
        process_usm(usm_file, opts, reporter, keys)

    assert not (tmp_path / "out" / "Cs_Test").exists()


def test_no_cleanup_keeps_intermediates(stub_stages, tmp_path, reporter):
    usm_file, opts, keys = make_run(tmp_path, no_cleanup=True)

    process_usm(usm_file, opts, reporter, keys)

    work_dir = tmp_path / "out" / "Cs_Test"
    assert (work_dir / "Cs_Test.ivf").is_file()
    assert (work_dir / "Cs_Test_0.hca").is_file()


def test_flat_lifts_the_mkv_and_drops_the_work_dir(stub_stages, tmp_path, reporter):
    usm_file, opts, keys = make_run(tmp_path, flat=True)

    process_usm(usm_file, opts, reporter, keys)

    assert (tmp_path / "out" / "Cs_Test.mkv").is_file()
    assert not (tmp_path / "out" / "Cs_Test").exists()


def test_flat_with_no_cleanup_keeps_the_work_dir(stub_stages, tmp_path, reporter):
    usm_file, opts, keys = make_run(tmp_path, no_cleanup=True, flat=True)

    process_usm(usm_file, opts, reporter, keys)

    assert (tmp_path / "out" / "Cs_Test.mkv").is_file()
    assert (tmp_path / "out" / "Cs_Test" / "Cs_Test.ivf").is_file()


@pytest.mark.parametrize(
    "flat, existing",
    [(False, "Cs_Test/Cs_Test.mkv"), (True, "Cs_Test.mkv")],
    ids=["nested", "flat"],
)
def test_skip_existing_stops_before_the_key_lookup(tmp_path, reporter, flat, existing):
    """-se is a resume switch: a missing key must not turn a skip into a failure."""
    usm_file, opts, _ = make_run(tmp_path, skip_existing=True, flat=flat)
    existing = tmp_path / "out" / existing
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(b"already here")

    process_usm(usm_file, opts, reporter, SimpleNamespace(decryption_key=forbid_call))

    assert ("job_skipped", {"file": "Cs_Test.usm", "reason": "exists"}) in reporter.events
    assert existing.read_bytes() == b"already here"


# --- hard-sub ---


def test_hard_sub_burns_the_default_language_with_no_soft_tracks(stub_stages, tmp_path, reporter):
    """The encode's ffmpeg gets the audio but no subtitle inputs and writes a .part that
    becomes the final .mkv, with no second mux."""
    usm_file, opts, keys = make_run(tmp_path, hard_sub=True, default_subtitle="JP")

    process_usm(usm_file, opts, reporter, keys)

    work_dir = tmp_path / "out" / "Cs_Test"
    ffmpeg_args = stub_stages.encode["ffmpeg_args"]
    assert stub_stages.encode["source"] == work_dir / "Cs_Test.ivf"
    assert stub_stages.encode["script"] is None
    assert "Cs_Test_JP.ass" in flag_value(ffmpeg_args, "-vf")
    assert input_files(ffmpeg_args) == [str(work_dir / "Cs_Test_0.flac")]
    assert ffmpeg_args[-1] == str(work_dir / "Cs_Test.mkv.part")
    assert (work_dir / "Cs_Test.mkv").read_bytes() == b"hevc"
    assert not (work_dir / "Cs_Test.mkv.part").exists()
    assert stub_stages.mux is None


def test_hard_sub_with_vapoursynth_encodes_once(stub_stages, tmp_path, reporter, monkeypatch):
    monkeypatch.setattr(pipeline, "find_vs_script", lambda stem: "default")
    usm_file, opts, keys = make_run(tmp_path, hard_sub=True, vapoursynth=True)

    process_usm(usm_file, opts, reporter, keys)

    assert stub_stages.encode["script"] == "default"
    assert "Cs_Test_EN.ass" in flag_value(stub_stages.encode["ffmpeg_args"], "-vf")
    assert stub_stages.mux is None


def test_hard_sub_without_the_default_language_skips_the_encode(
    stub_stages, tmp_path, reporter, monkeypatch, caplog
):
    monkeypatch.setattr(pipeline, "vapoursynth_filter", forbid_call)
    usm_file, opts, keys = make_run(tmp_path, hard_sub=True, default_subtitle="DE")

    process_usm(usm_file, opts, reporter, keys)

    assert "No DE subtitle" in caplog.text
    assert stub_stages.mux is not None


def test_failed_encode_falls_back_to_soft_subtitles(stub_stages, tmp_path, reporter):
    """The lossless copy with every language as a soft track is still a usable output, which
    is why the fallback mux runs like any other run and no .part is left behind."""
    stub_stages.ok = False
    usm_file, opts, keys = make_run(tmp_path, hard_sub=True)

    process_usm(usm_file, opts, reporter, keys)

    work_dir = tmp_path / "out" / "Cs_Test"
    assert stub_stages.mux is not None
    assert (work_dir / "Cs_Test.mkv").read_bytes() == b"mkv"
    assert not (work_dir / "Cs_Test.mkv.part").exists()


# --- subtitles ---


def test_subtitles_converted_to_ass(tmp_app_root, out_dir):
    write_subtitle("Cs_A", "DE")

    ass_files = process_subtitles("Cs_A", out_dir)

    assert [path.name for path in ass_files] == ["Cs_A_DE.ass"]
    assert (out_dir / "subs" / "Cs_A_DE.ass").is_file()


def test_empty_subtitle_named_not_converted(tmp_app_root, out_dir, caplog):
    """Upstream ships zero-byte .srt files for languages a cutscene was never localized
    into; naming them keeps the run from looking like it lost a track."""
    write_subtitle("Cs_A", "DE")
    write_subtitle("Cs_A", "EN", text="")

    ass_files = process_subtitles("Cs_A", out_dir)

    assert [path.name for path in ass_files] == ["Cs_A_DE.ass"]
    assert "Subtitles empty, skipping: English" in caplog.text


def test_unparseable_subtitle_skipped_quietly(tmp_app_root, out_dir, caplog):
    """Non-empty but yielding no dialogue is not "empty": listing it alongside the
    untranslated ones would mislead."""
    write_subtitle("Cs_A", "EN", text="nothing that parses")

    assert process_subtitles("Cs_A", out_dir) == []
    assert "Subtitles empty" not in caplog.text


def test_one_bad_subtitle_does_not_sink_the_rest(tmp_app_root, out_dir, monkeypatch, caplog):
    write_subtitle("Cs_A", "DE")
    write_subtitle("Cs_A", "EN")
    real_ass = pipeline.ASS

    def flaky(sub_file, lang, *args, **kwargs):
        if lang == "DE":
            raise ValueError("boom")
        return real_ass(sub_file, lang, *args, **kwargs)

    monkeypatch.setattr(pipeline, "ASS", flaky)

    ass_files = process_subtitles("Cs_A", out_dir)

    assert [path.name for path in ass_files] == ["Cs_A_EN.ass"]
    assert "Error processing subtitle" in caplog.text


# --- key recovery ---


def test_crack_reports_key_and_video_key(tmp_app_root, reporter, monkeypatch):
    """videoKey is the keys.json half: the combined key minus the filename hash."""
    combined = (calculate_key_from_filename("Cs_A") + 777) & 0xFFFFFFFFFFFFFF
    key_bytes = combined.to_bytes(8, "little")
    monkeypatch.setattr(
        pipeline, "crack_key", lambda f, r: Recovery((key_bytes[:4], key_bytes[4:]), "")
    )

    crack_usm(tmp_app_root / "Cs_A.usm", reporter)

    assert last_event(reporter, "crack") == {
        "file": "Cs_A.usm",
        "stem": "Cs_A",
        "key": combined,
        "video_key": 777,
        "reason": "",
    }


def test_crack_failure_keeps_the_same_event_shape(tmp_app_root, reporter, monkeypatch):
    monkeypatch.setattr(pipeline, "crack_key", lambda f, r: Recovery(None, "no IVF video stream"))

    crack_usm(tmp_app_root / "Cs_A.usm", reporter)

    assert last_event(reporter, "crack") == {
        "file": "Cs_A.usm",
        "stem": "Cs_A",
        "key": None,
        "video_key": None,
        "reason": "no IVF video stream",
    }


@pytest.mark.parametrize(
    "error, event",
    [
        (
            CharlotteError("Corrupt USM chunk: Cs_Bad.usm"),
            ("error", {"file": "Cs_Bad.usm", "message": "Corrupt USM chunk: Cs_Bad.usm"}),
        ),
        (Skipped(), ("job_skipped", {"file": "Cs_Bad.usm", "reason": "requested"})),
    ],
    ids=["unreadable", "skipped"],
)
def test_crack_batch_carries_on_past_a_failed_file(
    tmp_app_root, reporter, monkeypatch, error, event
):
    def crack(usm_file, reporter):
        if usm_file.name == "Cs_Bad.usm":
            raise error
        return Recovery((bytes(4), bytes(4)), "")

    monkeypatch.setattr(pipeline, "crack_key", crack)

    crack_all([tmp_app_root / "Cs_Bad.usm", tmp_app_root / "Cs_A.usm"], reporter)

    # The GUI attaches progress to the last job_start, which is why crack_all opens one per
    # file like process_usm does.
    assert [kind for kind, _ in reporter.events] == [
        "job_start",
        event[0],
        "job_start",
        "crack",
        "crack_summary",
    ]
    assert reporter.events[1] == event
    assert reporter.events[-1][1] == {"recovered": 1, "unrecovered": 1}


def test_crack_batch_stops_cleanly_on_cancel(tmp_app_root, reporter, monkeypatch):
    def crack(usm_file, reporter):
        raise Cancelled

    monkeypatch.setattr(pipeline, "crack_key", crack)

    crack_all([tmp_app_root / "Cs_A.usm", tmp_app_root / "Cs_B.usm"], reporter)

    assert reporter.events == [
        ("job_start", {"file": "Cs_A.usm", "stem": "Cs_A"}),
        ("cancelled", {"file": "Cs_A.usm"}),
    ]
