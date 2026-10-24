import os
import types

import pytest

from typer.testing import CliRunner

import main
import pipeline

from conftest import FakeReporter, forbid_call
from pipeline import Options
from utils.errors import Cancelled, CharlotteError, Skipped
from utils.version import __version__


runner = CliRunner()


def make_usm(directory, name="Cs_Test.usm"):
    path = directory / name
    path.write_bytes(b"")
    return path


@pytest.fixture
def pipeline_stub(monkeypatch):
    stub = types.SimpleNamespace(files=[], opts=None)

    def fake_process(usm_file, opts, keys, reporter):
        stub.files.append(usm_file)
        stub.opts = opts

    monkeypatch.setattr(pipeline, "process_usm", fake_process)
    monkeypatch.setattr(main, "Keys", lambda reporter, manual_key=None: None)
    monkeypatch.setattr(main, "sync_subtitles", lambda reporter: None)
    monkeypatch.setattr(main, "fetch_fonts", lambda: None)
    return stub


# --- modes that need no input files ---


def test_version_prints_and_exits(monkeypatch):
    monkeypatch.setattr(main, "collect_files", forbid_call)
    result = runner.invoke(main.app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_update_runs_without_input_files(monkeypatch):
    called = []
    monkeypatch.setattr(main, "run_update", lambda reporter, json_mode: called.append(json_mode))
    monkeypatch.setattr(main, "collect_files", forbid_call)

    assert runner.invoke(main.app, ["--update"]).exit_code == 0
    assert called == [False]


@pytest.mark.parametrize(
    "extra", [["Cs_A.usm"], ["--probe"], ["--crack"], ["--capture"], ["--key", "1"]]
)
def test_update_rejects_every_other_mode(monkeypatch, extra):
    """--update replaces the binary and exits. Combining it with a run would leave the engine
    mid-batch on a version that just got swapped out from under it."""
    monkeypatch.setattr(main, "run_update", forbid_call)
    assert runner.invoke(main.app, ["--update", *extra]).exit_code == 1


@pytest.mark.parametrize("extra", [["Cs_A.usm"], ["--probe"], ["--crack"], ["--key", "1"]])
def test_capture_rejects_every_other_mode(monkeypatch, extra):
    monkeypatch.setattr(main, "KeyCapture", forbid_call)
    assert runner.invoke(main.app, ["--capture", *extra]).exit_code == 1


@pytest.mark.parametrize("flags", [["--json"], ["--capture"]])
def test_pipe_is_only_for_a_gui_capture(monkeypatch, flags):
    monkeypatch.setattr(main, "open_pipe", forbid_call)
    assert runner.invoke(main.app, [*flags, "--pipe", "charlotte-x"]).exit_code == 1


def test_the_guis_capture_command_reaches_the_pipe_and_the_language(monkeypatch):
    """CaptureClient in MonsieurVerite starts the engine with exactly these arguments, and a
    renamed flag here would fail every capture the GUI starts."""
    opened = []

    def open_pipe(name):
        opened.append((name, os.environ["CHARLOTTE_LANG"]))
        return FakeReporter()

    monkeypatch.setattr(main, "open_pipe", open_pipe)
    monkeypatch.setattr(main, "is_admin", lambda: True)
    monkeypatch.setattr(main, "Keys", lambda reporter: types.SimpleNamespace(data={}))
    monkeypatch.setattr(
        main, "KeyCapture", lambda data, reporter, legacy: types.SimpleNamespace(run=lambda: None)
    )
    monkeypatch.setenv("CHARLOTTE_LANG", "en-US")

    args = ["--capture", "--json", "--pipe", "charlotte-capture-1", "--lang", "ja-JP"]
    try:
        assert runner.invoke(main.app, args).exit_code == 0
    finally:
        main.strings.cache_clear()
    assert opened == [("charlotte-capture-1", "ja-JP")]


def test_capture_from_the_gui_without_admin_rights_fails_instead_of_prompting(monkeypatch):
    """Only a console run relaunches itself through UAC, because the GUI could not read the
    output of a process it did not start."""
    monkeypatch.setattr(main, "is_admin", lambda: False)
    monkeypatch.setattr(main, "relaunch_as_admin", forbid_call)
    monkeypatch.setattr(main, "KeyCapture", forbid_call)

    result = runner.invoke(main.app, ["--capture", "--json"])
    assert result.exit_code == 1
    assert '{"type":"error","file":"","message":' in result.stdout


def test_no_input_is_an_error():
    assert runner.invoke(main.app, []).exit_code == 1


# --- input collection ---


@pytest.mark.parametrize(
    "bad_input", ["a.txt", "", "nope.usm"], ids=["non-usm", "dir-without-usm", "missing"]
)
def test_rejects_unusable_input(tmp_path, bad_input):
    (tmp_path / "a.txt").write_bytes(b"")
    assert runner.invoke(main.app, [str(tmp_path / bad_input)]).exit_code == 1


def test_duplicate_inputs_processed_once(pipeline_stub, tmp_path):
    usm = make_usm(tmp_path)
    args = [str(usm), str(usm), "-o", str(tmp_path / "out")]
    assert runner.invoke(main.app, args).exit_code == 0
    assert pipeline_stub.files == [usm]


def test_directory_glob_sorted(pipeline_stub, tmp_path):
    second = make_usm(tmp_path, "Cs_B.usm")
    first = make_usm(tmp_path, "Cs_A.usm")
    args = [str(tmp_path), "-o", str(tmp_path / "out")]
    assert runner.invoke(main.app, args).exit_code == 0
    assert pipeline_stub.files == [first, second]


# --- flag validation ---


def test_key_requires_single_input(tmp_path):
    files = [make_usm(tmp_path, "Cs_A.usm"), make_usm(tmp_path, "Cs_B.usm")]
    result = runner.invoke(main.app, [*map(str, files), "--key", "1"])
    assert result.exit_code == 1


@pytest.mark.parametrize(
    "flags", [["--crack", "--probe"], ["--crack", "--key", "1"], ["--probe", "--key", "1"]]
)
def test_conflicting_modes_rejected(tmp_path, flags):
    usm = make_usm(tmp_path)
    assert runner.invoke(main.app, [str(usm), *flags]).exit_code == 1


def test_rejected_flags_reach_the_gui(tmp_path):
    usm = make_usm(tmp_path)
    result = runner.invoke(main.app, [str(usm), "--crack", "--probe", "--json"])
    assert '{"type":"error","file":"","message":' in result.stdout


def test_invalid_choice_flag_is_usage_error(tmp_path):
    usm = make_usm(tmp_path)
    assert runner.invoke(main.app, [str(usm), "-da", "xx"]).exit_code == 2


def test_choice_flags_normalized_into_options(pipeline_stub, tmp_path):
    usm = make_usm(tmp_path)
    args = [
        str(usm),
        "-o", str(tmp_path / "out"),
        "--default-audio", "EN",
        "--default-sub", "chs",
        "-ac", "OPUS",
    ]  # fmt: skip
    assert runner.invoke(main.app, args).exit_code == 0

    opts = pipeline_stub.opts
    assert opts.default_audio == "en"
    assert opts.default_subtitle == "CHS"
    assert opts.audio_codec == "opus"


def test_the_guis_convert_command_reaches_the_options(pipeline_stub, tmp_path):
    """MainViewModel and RunOptions.ToArguments in MonsieurVerite build this command, and a
    renamed flag here would fail every conversion the GUI starts."""
    usm = make_usm(tmp_path)
    args = [
        "--json",
        "--output", str(tmp_path / "out"),
        "--default-audio", "en",
        "--default-sub", "JP",
        "--audio-codec", "opus",
        "--no-cleanup",
        "--skip-existing",
        "--flat",
        "--vapoursynth",
        "--hard-sub",
        "--crf", "18",
        "--preset", "medium",
        "--x265-params", "aq-mode=3",
        "--key", "12345",
        str(usm),
    ]  # fmt: skip
    assert runner.invoke(main.app, args).exit_code == 0

    assert pipeline_stub.opts == Options(
        output=tmp_path / "out",
        no_cleanup=True,
        vapoursynth=True,
        crf=18.0,
        preset="medium",
        x265_params="aq-mode=3",
        fonts=None,
        default_audio="en",
        default_subtitle="JP",
        audio_codec="opus",
        skip_existing=True,
        flat=True,
        hard_sub=True,
    )


def test_default_subtitle_comes_out_canonical(pipeline_stub, tmp_path):
    """The default "en" runs through the normalizer too, and a lowercase code would match no
    subtitle file."""
    usm = make_usm(tmp_path)
    assert runner.invoke(main.app, [str(usm), "-o", str(tmp_path / "out")]).exit_code == 0
    assert pipeline_stub.opts.default_subtitle == "EN"


# --- run outcomes ---


@pytest.mark.parametrize(
    "error, event, processed, exit_code",
    [
        (
            Skipped(),
            '{"type":"job_skipped","file":"Cs_A.usm","reason":"requested"}',
            ["Cs_A", "Cs_B"],
            0,
        ),
        (
            CharlotteError("boom"),
            '{"type":"error","file":"Cs_A.usm","message":"boom"}',
            ["Cs_A", "Cs_B"],
            1,
        ),
        (Cancelled(), '{"type":"cancelled","file":"Cs_A.usm"}', ["Cs_A"], 0),
    ],
    ids=["skip", "failure", "cancel"],
)
def test_batch_outcome_of_a_stopped_file(
    pipeline_stub, monkeypatch, tmp_path, error, event, processed, exit_code
):
    seen = []

    def process(usm_file, opts, keys, reporter):
        seen.append(usm_file.stem)
        if usm_file.name == "Cs_A.usm":
            raise error

    monkeypatch.setattr(pipeline, "process_usm", process)
    files = [make_usm(tmp_path, "Cs_A.usm"), make_usm(tmp_path, "Cs_B.usm")]
    result = runner.invoke(main.app, [*map(str, files), "-o", str(tmp_path / "out"), "--json"])

    assert result.exit_code == exit_code
    assert seen == processed
    assert event in result.stdout


def test_cancel_after_a_failure_still_exits_1(pipeline_stub, monkeypatch, tmp_path):
    def process(usm_file, opts, keys, reporter):
        if usm_file.name == "Cs_A.usm":
            raise CharlotteError("boom")
        raise Cancelled

    monkeypatch.setattr(pipeline, "process_usm", process)
    files = [make_usm(tmp_path, "Cs_A.usm"), make_usm(tmp_path, "Cs_B.usm")]
    result = runner.invoke(main.app, [*map(str, files), "-o", str(tmp_path / "out"), "--json"])

    assert result.exit_code == 1
    assert '{"type":"cancelled","file":"Cs_B.usm"}' in result.stdout


def test_probe_shares_one_keys_and_carries_on_past_a_failed_file(monkeypatch, tmp_path):
    """One Keys serves the whole run and probe_usm gets the object rather than a snapshot of
    its data, which is how an update accepted partway through reaches the later files."""
    keys = types.SimpleNamespace(data={"list": []})
    probed = []

    def probe(usm_file, keys, reporter):
        probed.append((usm_file, keys))
        if usm_file.name == "Cs_A.usm":
            raise OSError("locked")

    monkeypatch.setattr(pipeline, "probe_usm", probe)
    monkeypatch.setattr(main, "Keys", lambda reporter: keys)
    monkeypatch.setattr(main, "sync_subtitles", forbid_call)
    monkeypatch.setattr(pipeline, "process_usm", forbid_call)

    files = [make_usm(tmp_path, "Cs_A.usm"), make_usm(tmp_path, "Cs_B.usm")]
    assert runner.invoke(main.app, [*map(str, files), "--probe"]).exit_code == 0
    assert probed == [(files[0], keys), (files[1], keys)]


def test_crack_skips_keys_and_pipeline(monkeypatch, tmp_path):
    cracked = []
    monkeypatch.setattr(main, "crack_all", lambda files, reporter: cracked.extend(files))
    monkeypatch.setattr(main, "Keys", forbid_call)
    monkeypatch.setattr(main, "sync_subtitles", forbid_call)
    monkeypatch.setattr(pipeline, "process_usm", forbid_call)

    usm = make_usm(tmp_path)
    assert runner.invoke(main.app, [str(usm), "--crack"]).exit_code == 0
    assert cracked == [usm]
