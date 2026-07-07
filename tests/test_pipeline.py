import pipeline

from pipeline import probe_usm
from resources.subtitles import local_subtitle_path


KEYS_DATA = {"list": [{"videoKey": 111, "videos": ["Cs_A"]}]}


def write_subtitle(stem, lang):
    path = local_subtitle_path(stem, lang)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n", encoding="utf-8")


def last_probe_event(reporter):
    kind, data = reporter.events[-1]
    assert kind == "probe"
    return data


def test_probe_reports_available(tmp_app_root, reporter, monkeypatch):
    monkeypatch.setattr(pipeline, "find_vs_script", lambda stem: "Cs_A")
    write_subtitle("Cs_A", "EN")
    write_subtitle("Cs_A", "JP")

    probe_usm(tmp_app_root / "Cs_A.usm", KEYS_DATA, reporter)

    assert last_probe_event(reporter) == {
        "file": "Cs_A.usm",
        "stem": "Cs_A",
        "key": True,
        "subtitles": ["EN", "JP"],
        "vs_script": "Cs_A",
    }


def test_probe_reports_missing_and_never_prompts(tmp_app_root, reporter, monkeypatch):
    monkeypatch.setattr(pipeline, "find_vs_script", lambda stem: None)

    probe_usm(tmp_app_root / "Cs_A.usm", {}, reporter)

    data = last_probe_event(reporter)
    assert data["key"] is False
    assert data["subtitles"] == []
    assert data["vs_script"] is None
    assert reporter.prompts == []


def test_probe_remaps_subtitle_stem_only(tmp_app_root, reporter, monkeypatch):
    """BASENAME_FIXES applies to the subtitle lookup, while the key and the VapourSynth
    script keep using the original stem."""
    seen_vs_stems = []
    # list.append takes the stem and returns None, doubling as a "no script found" stub.
    monkeypatch.setattr(pipeline, "find_vs_script", seen_vs_stems.append)
    write_subtitle("Cs_DQAQ200211_WanYeXianVideo", "EN")

    probe_usm(tmp_app_root / "Cs_200211_WanYeXianVideo.usm", {}, reporter)

    data = last_probe_event(reporter)
    assert data["stem"] == "Cs_200211_WanYeXianVideo"
    assert data["subtitles"] == ["EN"]  # found under the remapped stem
    assert seen_vs_stems == ["Cs_200211_WanYeXianVideo"]
