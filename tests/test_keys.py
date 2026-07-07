import orjson
import pytest

import utils.keys

from utils.errors import CharlotteError
from utils.keys import (
    find_key_from_file,
    get_key,
    load_local_keys,
)


FLAT_KEYS = {"list": [{"videoKey": 111, "videos": ["Cs_A", "Cs_B"]}]}
GROUPED_KEYS = {"list": [{"videoGroups": [{"videoKey": 222, "videos": ["Cs_C"]}]}]}
UPSTREAM_WITH_NEW_KEY = {"list": FLAT_KEYS["list"] + [{"videoKey": 333, "videos": ["Cs_New"]}]}


def write_keys(root, data):
    path = root / "keys.json"
    path.write_bytes(orjson.dumps(data))
    return path


def forbid_fetch():
    pytest.fail("Unexpected upstream fetch.")


# --- find_key_from_file ---


def test_find_key_flat():
    assert find_key_from_file(FLAT_KEYS, "Cs_B") == 111


def test_find_key_grouped():
    assert find_key_from_file(GROUPED_KEYS, "Cs_C") == 222


def test_find_key_missing():
    assert find_key_from_file(FLAT_KEYS, "Cs_X") is None
    assert find_key_from_file(GROUPED_KEYS, "Cs_X") is None
    assert find_key_from_file({}, "Cs_A") is None


# --- load_local_keys ---


def test_load_local_keys_roundtrip(tmp_app_root):
    write_keys(tmp_app_root, FLAT_KEYS)
    assert load_local_keys() == FLAT_KEYS


def test_load_local_keys_missing_or_corrupt(tmp_app_root):
    assert load_local_keys() == {}
    (tmp_app_root / "keys.json").write_bytes(b"not json")
    assert load_local_keys() == {}


# --- get_key ---


def test_get_key_local_hit_skips_network(tmp_app_root, reporter, monkeypatch):
    write_keys(tmp_app_root, FLAT_KEYS)
    monkeypatch.setattr(utils.keys, "fetch_upstream_keys", forbid_fetch)
    assert get_key("Cs_A", reporter) == 111
    assert reporter.prompts == []


def test_get_key_missing_file_fetch_fails(reporter, monkeypatch):
    monkeypatch.setattr(utils.keys, "fetch_upstream_keys", lambda: None)
    with pytest.raises(CharlotteError):
        get_key("Cs_A", reporter)


def test_get_key_missing_file_fetched_and_saved(tmp_app_root, reporter, monkeypatch):
    monkeypatch.setattr(utils.keys, "fetch_upstream_keys", lambda: orjson.dumps(FLAT_KEYS))
    assert get_key("Cs_A", reporter) == 111
    assert load_local_keys() == FLAT_KEYS


def test_get_key_upstream_identical_returns_none(tmp_app_root, reporter, monkeypatch):
    write_keys(tmp_app_root, FLAT_KEYS)
    monkeypatch.setattr(utils.keys, "fetch_upstream_keys", lambda: orjson.dumps(FLAT_KEYS))
    assert get_key("Cs_X", reporter) is None
    assert reporter.prompts == []


def test_get_key_new_upstream_key_accepted(tmp_app_root, reporter, monkeypatch):
    write_keys(tmp_app_root, FLAT_KEYS)
    monkeypatch.setattr(
        utils.keys, "fetch_upstream_keys", lambda: orjson.dumps(UPSTREAM_WITH_NEW_KEY)
    )
    reporter.answer = True

    assert get_key("Cs_New", reporter) == 333
    assert len(reporter.prompts) == 1
    # Accepting the prompt overwrites the local keys.json with the upstream copy.
    assert load_local_keys() == UPSTREAM_WITH_NEW_KEY


def test_get_key_new_upstream_key_declined(tmp_app_root, reporter, monkeypatch):
    write_keys(tmp_app_root, FLAT_KEYS)
    monkeypatch.setattr(
        utils.keys, "fetch_upstream_keys", lambda: orjson.dumps(UPSTREAM_WITH_NEW_KEY)
    )
    reporter.answer = False

    assert get_key("Cs_New", reporter) is None
    assert load_local_keys() == FLAT_KEYS


def test_get_key_corrupt_local_recovers_from_upstream(tmp_app_root, reporter, monkeypatch):
    (tmp_app_root / "keys.json").write_bytes(b"not json")
    monkeypatch.setattr(utils.keys, "fetch_upstream_keys", lambda: orjson.dumps(FLAT_KEYS))
    reporter.answer = True
    assert get_key("Cs_A", reporter) == 111
