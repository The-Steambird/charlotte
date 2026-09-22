import orjson

import resources.keys

from conftest import forbid_call
from resources.keys import (
    Keys,
    calculate_key_from_filename,
    find_video_key,
    find_video_version,
    load_local_keys,
)


FLAT_KEYS = {"list": [{"version": "2.0", "videoKey": 111, "videos": ["Cs_A", "Cs_B"]}]}
GROUPED_KEYS = {
    "list": [{"version": "5.3", "videoGroups": [{"videoKey": 222, "videos": ["Cs_C"]}]}]
}
UPSTREAM_WITH_NEW_KEY = {
    "list": FLAT_KEYS["list"] + [{"videoKey": 333, "videos": ["Cs_New", "Cs_New2"]}]
}


def write_keys(root, data):
    path = root / "keys.json"
    path.write_bytes(orjson.dumps(data))
    return path


# --- find_video_key ---


def test_find_key_in_flat_and_grouped_lists():
    assert find_video_key(FLAT_KEYS, "Cs_B") == 111
    assert find_video_key(GROUPED_KEYS, "Cs_C") == 222
    assert find_video_key(FLAT_KEYS, "Cs_X") is None
    assert find_video_key(GROUPED_KEYS, "Cs_X") is None
    assert find_video_key({}, "Cs_A") is None
    # A group carrying no video list at all is a miss, not a KeyError.
    assert find_video_key({"list": [{"videoKey": 1}]}, "Cs_A") is None


def test_find_version_comes_from_the_entry_not_the_group():
    assert find_video_version(FLAT_KEYS, "Cs_B") == "2.0"
    assert find_video_version(GROUPED_KEYS, "Cs_C") == "5.3"
    assert find_video_version(FLAT_KEYS, "Cs_X") is None
    assert find_video_version({"list": [{"videoKey": 1, "videos": ["Cs_A"]}]}, "Cs_A") is None


# --- load_local_keys ---


def test_load_local_keys_roundtrip(tmp_app_root):
    write_keys(tmp_app_root, FLAT_KEYS)
    assert load_local_keys() == FLAT_KEYS


def test_load_local_keys_missing_or_corrupt(tmp_app_root):
    assert load_local_keys() == {}
    (tmp_app_root / "keys.json").write_bytes(b"not json")
    assert load_local_keys() == {}


# --- Keys ---


def test_local_hit_skips_network(tmp_app_root, reporter, monkeypatch):
    write_keys(tmp_app_root, FLAT_KEYS)
    monkeypatch.setattr(resources.keys, "fetch_upstream_keys", forbid_call)
    assert Keys(reporter).get("Cs_A") == 111
    assert reporter.prompts == []


def test_manual_key_skips_disk_and_network(reporter, monkeypatch):
    monkeypatch.setattr(resources.keys, "fetch_upstream_keys", forbid_call)
    assert Keys(reporter, manual_key=42).get("Cs_Anything") == 42
    assert reporter.prompts == []


def test_missing_file_and_no_upstream_is_not_fatal(reporter, monkeypatch):
    """The result is an empty key set rather than an exception, because every file can
    still fall back to recovery."""
    monkeypatch.setattr(resources.keys, "fetch_upstream_keys", lambda: None)
    assert Keys(reporter).get("Cs_A") is None


def test_missing_file_fetched_and_saved(tmp_app_root, reporter, monkeypatch):
    monkeypatch.setattr(resources.keys, "fetch_upstream_keys", lambda: orjson.dumps(FLAT_KEYS))
    assert Keys(reporter).get("Cs_A") == 111
    assert load_local_keys() == FLAT_KEYS


def test_upstream_identical_returns_none(tmp_app_root, reporter, monkeypatch):
    write_keys(tmp_app_root, FLAT_KEYS)
    monkeypatch.setattr(resources.keys, "fetch_upstream_keys", lambda: orjson.dumps(FLAT_KEYS))
    assert Keys(reporter).get("Cs_X") is None
    assert reporter.prompts == []


def test_new_upstream_key_accepted_once_for_the_run(tmp_app_root, reporter, monkeypatch):
    write_keys(tmp_app_root, FLAT_KEYS)
    monkeypatch.setattr(
        resources.keys, "fetch_upstream_keys", lambda: orjson.dumps(UPSTREAM_WITH_NEW_KEY)
    )
    reporter.answer = True

    keys = Keys(reporter)
    assert keys.get("Cs_New") == 333
    assert keys.get("Cs_New2") == 333
    assert len(reporter.prompts) == 1
    assert load_local_keys() == UPSTREAM_WITH_NEW_KEY


def test_new_upstream_key_declined(tmp_app_root, reporter, monkeypatch):
    write_keys(tmp_app_root, FLAT_KEYS)
    monkeypatch.setattr(
        resources.keys, "fetch_upstream_keys", lambda: orjson.dumps(UPSTREAM_WITH_NEW_KEY)
    )
    reporter.answer = False

    keys = Keys(reporter)
    assert keys.get("Cs_New") is None
    assert load_local_keys() == FLAT_KEYS
    assert keys.get("Cs_New") is None
    assert len(reporter.prompts) == 1


# --- decryption_key ---


def test_decryption_key_splits_the_combined_key(tmp_app_root, reporter, monkeypatch):
    write_keys(tmp_app_root, FLAT_KEYS)
    monkeypatch.setattr(resources.keys, "fetch_upstream_keys", forbid_call)

    key_pair = Keys(reporter).decryption_key("Cs_A")

    assert key_pair is not None
    key1, key2 = key_pair
    combined = (calculate_key_from_filename("Cs_A") + 111) & 0xFFFFFFFFFFFFFF
    assert key1 + key2 == combined.to_bytes(8, "little")
