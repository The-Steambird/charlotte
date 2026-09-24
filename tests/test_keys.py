import orjson

import resources.keys

from conftest import forbid_call
from resources.keys import (
    Keys,
    calculate_key_from_filename,
    find_video_key,
    find_video_version,
)


FLAT_KEYS = {"list": [{"version": "2.0", "videoKey": 111, "videos": ["Cs_A", "Cs_B"]}]}
GROUPED_KEYS = {
    "list": [{"version": "5.3", "videoGroups": [{"videoKey": 222, "videos": ["Cs_C"]}]}]
}
UPSTREAM_WITH_NEW_KEY = {
    "list": FLAT_KEYS["list"] + [{"videoKey": 333, "videos": ["Cs_New", "Cs_New2"]}]
}
AES_HEX = "f9e9e1c5cf3a68deffa0a4d3df665836"
# audioKey 5 split into the two halves with no filename term added.
STREAM_KEYS_OF_5 = (bytes([5, 0, 0, 0]), bytes(4), bytes.fromhex(AES_HEX))
STREAM_KEYS = {
    "list": [
        {
            "version": "7.1",
            "videoGroups": [
                {"audioKey": 5, "aesKey": AES_HEX, "videos": ["Cs_Both"]},
                {"audioKey": 6, "videos": ["Cs_AudioOnly"]},
            ],
        }
    ]
}


def write_keys(root, data):
    path = root / "keys.json"
    path.write_bytes(orjson.dumps(data))
    return path


def read_keys(root):
    return orjson.loads((root / "keys.json").read_bytes())


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


# --- Keys ---


def test_local_hit_skips_network(tmp_app_root, reporter, monkeypatch):
    write_keys(tmp_app_root, FLAT_KEYS)
    monkeypatch.setattr(resources.keys, "fetch_upstream_keys", forbid_call)
    assert Keys(reporter).get("Cs_A") == 111
    assert reporter.prompts == []


def test_manual_key_skips_disk_and_network(reporter, monkeypatch):
    monkeypatch.setattr(resources.keys, "fetch_upstream_keys", forbid_call)
    assert Keys(reporter, manual_key="42").get("Cs_Anything") == 42
    stream_keys = Keys(reporter, manual_key=f"5 : {AES_HEX}").stream_keys("Cs_Anything")
    assert stream_keys == STREAM_KEYS_OF_5
    assert reporter.prompts == []


def test_stream_keys_need_both_fields_of_the_group(tmp_app_root, reporter, monkeypatch):
    write_keys(tmp_app_root, STREAM_KEYS)
    monkeypatch.setattr(resources.keys, "fetch_upstream_keys", lambda: None)

    keys = Keys(reporter)

    assert keys.stream_keys("Cs_Both") == STREAM_KEYS_OF_5
    assert keys.stream_keys("Cs_AudioOnly") is None


def test_missing_file_and_no_upstream_is_not_fatal(reporter, monkeypatch):
    """The result is an empty key set rather than an exception, because every file can
    still fall back to recovery."""
    monkeypatch.setattr(resources.keys, "fetch_upstream_keys", lambda: None)
    assert Keys(reporter).get("Cs_A") is None


def test_missing_file_fetched_and_saved(tmp_app_root, reporter, monkeypatch):
    monkeypatch.setattr(resources.keys, "fetch_upstream_keys", lambda: orjson.dumps(FLAT_KEYS))
    assert Keys(reporter).get("Cs_A") == 111
    assert read_keys(tmp_app_root) == FLAT_KEYS


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
    assert read_keys(tmp_app_root) == UPSTREAM_WITH_NEW_KEY


def test_new_upstream_key_declined(tmp_app_root, reporter, monkeypatch):
    write_keys(tmp_app_root, FLAT_KEYS)
    monkeypatch.setattr(
        resources.keys, "fetch_upstream_keys", lambda: orjson.dumps(UPSTREAM_WITH_NEW_KEY)
    )
    reporter.answer = False

    keys = Keys(reporter)
    assert keys.get("Cs_New") is None
    assert read_keys(tmp_app_root) == FLAT_KEYS
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
