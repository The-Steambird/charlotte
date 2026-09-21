from pathlib import Path
from types import SimpleNamespace

import pytest

import pipeline
import resources.keys

from conftest import FakeReporter, chunk, forbid_call
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
        "x265_params": "",
    }
    return Options(**defaults | overrides)


def make_run(tmp_path, chunks=None, **overrides):
    """A one-video one-audio USM (unless `chunks` says otherwise) plus the options and
    keys to process it."""
    usm_file = tmp_path / "Cs_Test.usm"
    usm_file.write_bytes(chunks or chunk(b"@SFV", b"video") + chunk(b"@SFA", b"audio"))
    keys = SimpleNamespace(decryption_key=lambda stem: (bytes(4), bytes(4)))
    return usm_file, make_options(tmp_path, **overrides), keys


# --- probe ---


def test_probe_reports_available(tmp_app_root, reporter, monkeypatch):
    monkeypatch.setattr(pipeline, "find_vs_script", lambda stem: "Cs_A")
    write_subtitle("Cs_A", "EN")
    write_subtitle("Cs_A", "JP")

    probe_usm(tmp_app_root / "Cs_A.usm", KEYS_DATA, reporter)

    assert last_event(reporter, "probe") == {
        "file": "Cs_A.usm",
        "stem": "Cs_A",
        "key": True,
        "version": "5.3",
        "subtitles": ["EN", "JP"],
        "vs_script": "Cs_A",
    }


def test_probe_reports_missing_and_never_prompts(tmp_app_root, reporter, monkeypatch):
    monkeypatch.setattr(pipeline, "find_vs_script", lambda stem: None)

    probe_usm(tmp_app_root / "Cs_A.usm", {}, reporter)

    data = last_event(reporter, "probe")
    assert data["key"] is False
    assert data["version"] is None
    assert data["subtitles"] == []
    assert data["vs_script"] is None
    assert reporter.prompts == []


def test_probe_remaps_subtitle_stem_only(tmp_app_root, reporter, monkeypatch):
    """BASENAME_FIXES applies to the subtitle lookup; the key and VapourSynth script
    keep the original stem."""
    seen_vs_stems = []
    monkeypatch.setattr(pipeline, "find_vs_script", seen_vs_stems.append)  # returns None
    write_subtitle("Cs_DQAQ200211_WanYeXianVideo", "EN")

    probe_usm(tmp_app_root / "Cs_200211_WanYeXianVideo.usm", {}, reporter)

    data = last_event(reporter, "probe")
    assert data["stem"] == "Cs_200211_WanYeXianVideo"
    assert data["subtitles"] == ["EN"]
    assert seen_vs_stems == ["Cs_200211_WanYeXianVideo"]


# --- cancel and key fallback ---


class StopDuringDemux(FakeReporter):
    """Quiet through process_usm's two pre-demux checkpoints, then cancels (or skips) at the
    first checkpoint inside the demux loop."""

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


# --- full run: output layout and cleanup ---


@pytest.fixture
def stub_stages(monkeypatch):
    """Run process_usm from demux to cleanup with the ffmpeg/VapourSynth stages replaced;
    the mux stub writes the .mkv the output handling then moves."""
    monkeypatch.setattr(pipeline, "process_audio", lambda *args, **kwargs: [])
    monkeypatch.setattr(pipeline, "process_subtitles", lambda **kwargs: [])
    monkeypatch.setattr(
        pipeline,
        "mux",
        lambda output_path, **kwargs: (output_path / f"{output_path.name}.mkv").write_bytes(b"mkv"),
    )


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


@pytest.fixture
def encode_stub(monkeypatch):
    """Like stub_stages, but with one audio track and EN plus JP subtitles on disk so the
    real mux_args runs. The VapourSynth stage records what it was handed and always writes
    the .part the way ffmpeg does whether or not it ends `ok`, and the fallback mux records
    its call."""
    calls = SimpleNamespace(ok=True, encode=None, mux=None)

    def audio(hca_files, *args, **kwargs):
        flac = hca_files[0].with_suffix(".flac")
        flac.write_bytes(b"flac")
        return [flac]

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

    def mux(output_path, **kwargs):
        calls.mux = kwargs
        (output_path / f"{output_path.name}.mkv").write_bytes(b"mkv")

    monkeypatch.setattr(pipeline, "process_audio", audio)
    monkeypatch.setattr(pipeline, "process_subtitles", subtitles)
    monkeypatch.setattr(pipeline, "vapoursynth_filter", encode)
    monkeypatch.setattr(pipeline, "mux", mux)
    return calls


def test_hard_sub_burns_the_default_language_with_no_soft_tracks(encode_stub, tmp_path, reporter):
    """The encode's ffmpeg gets the audio but no subtitle inputs and writes a .part that
    becomes the final .mkv, with no second mux."""
    usm_file, opts, keys = make_run(tmp_path, hard_sub=True, default_subtitle="JP")

    process_usm(usm_file, opts, reporter, keys)

    work_dir = tmp_path / "out" / "Cs_Test"
    ffmpeg_args = encode_stub.encode["ffmpeg_args"]
    assert encode_stub.encode["source"] == work_dir / "Cs_Test.ivf"
    assert encode_stub.encode["script"] is None
    assert encode_stub.encode["subtitle"] == work_dir / "subs" / "Cs_Test_JP.ass"
    assert not [arg for arg in ffmpeg_args if arg.endswith(".ass")]
    assert str(work_dir / "Cs_Test_0.flac") in ffmpeg_args
    assert ffmpeg_args[-1] == str(work_dir / "Cs_Test.mkv.part")
    assert (work_dir / "Cs_Test.mkv").read_bytes() == b"hevc"
    assert not (work_dir / "Cs_Test.mkv.part").exists()
    assert encode_stub.mux is None


def test_hard_sub_with_vapoursynth_encodes_once(encode_stub, tmp_path, reporter, monkeypatch):
    monkeypatch.setattr(pipeline, "find_vs_script", lambda stem: "default")
    usm_file, opts, keys = make_run(tmp_path, hard_sub=True, vapoursynth=True)

    process_usm(usm_file, opts, reporter, keys)

    assert encode_stub.encode["script"] == "default"
    assert encode_stub.encode["subtitle"].name == "Cs_Test_EN.ass"
    assert encode_stub.mux is None


def test_hard_sub_without_the_default_language_skips_the_encode(
    encode_stub, tmp_path, reporter, monkeypatch, caplog
):
    monkeypatch.setattr(pipeline, "vapoursynth_filter", forbid_call)
    usm_file, opts, keys = make_run(tmp_path, hard_sub=True, default_subtitle="DE")

    process_usm(usm_file, opts, reporter, keys)

    assert "No DE subtitle" in caplog.text
    assert encode_stub.mux is not None


def test_failed_encode_falls_back_to_soft_subtitles(encode_stub, tmp_path, reporter):
    """The lossless copy with every language as a soft track is still a usable output, which
    is why the fallback mux runs like any other run and no .part is left behind."""
    encode_stub.ok = False
    usm_file, opts, keys = make_run(tmp_path, hard_sub=True)

    process_usm(usm_file, opts, reporter, keys)

    work_dir = tmp_path / "out" / "Cs_Test"
    assert encode_stub.mux is not None
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


def test_crack_batch_continues_past_an_unreadable_file(tmp_app_root, reporter, monkeypatch):
    def crack(usm_file, reporter):
        if usm_file.name == "Cs_Bad.usm":
            raise CharlotteError("Corrupt USM chunk: Cs_Bad.usm")
        return Recovery((bytes(4), bytes(4)), "")

    monkeypatch.setattr(pipeline, "crack_key", crack)

    crack_all([tmp_app_root / "Cs_Bad.usm", tmp_app_root / "Cs_A.usm"], reporter)

    # job_start per file, like process_usm, so the GUI has something to attach progress to.
    assert [kind for kind, _ in reporter.events] == [
        "job_start",
        "error",
        "job_start",
        "crack",
        "crack_summary",
    ]
    assert reporter.events[-1][1] == {"recovered": 1, "unrecovered": 1}


def test_crack_batch_carries_on_after_skip(tmp_app_root, reporter, monkeypatch):
    def crack(usm_file, reporter):
        if usm_file.name == "Cs_A.usm":
            raise Skipped
        return Recovery(key=(b"" * 4, b"" * 4), reason="")

    monkeypatch.setattr(pipeline, "crack_key", crack)

    crack_all([tmp_app_root / "Cs_A.usm", tmp_app_root / "Cs_B.usm"], reporter)

    assert [kind for kind, _ in reporter.events] == [
        "job_start",
        "job_skipped",
        "job_start",
        "crack",
        "crack_summary",
    ]
    assert reporter.events[1][1] == {"file": "Cs_A.usm", "reason": "requested"}
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
