import pytest

import resources.fonts

from resources.fonts import fetch_font


@pytest.fixture
def game_fonts(tmp_path, monkeypatch):
    """Point the registry lookup at a fake game font directory."""
    game = tmp_path / "game_fonts"
    game.mkdir()
    for name in ("ja-jp.ttf", "zh-cn.ttf"):
        (game / name).write_bytes(b"game:" + name.encode())
    monkeypatch.setattr(resources.fonts, "game_font_dir", lambda: game)
    return game


def local_font(tmp_app_root, name, content=b"local"):
    path = tmp_app_root / "font" / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(content)
    return path


def test_local_fonts_skip_game_lookup(tmp_app_root, monkeypatch):
    local_font(tmp_app_root, "ja-jp.ttf")
    local_font(tmp_app_root, "zh-cn.ttf")
    monkeypatch.setattr(
        resources.fonts, "game_font_dir", lambda: pytest.fail("registry looked up needlessly")
    )
    assert [font.name for font in fetch_font()] == ["ja-jp.ttf", "zh-cn.ttf"]


def test_missing_fonts_copied_from_game(tmp_app_root, game_fonts):
    fonts = fetch_font()
    assert [font.name for font in fonts] == ["ja-jp.ttf", "zh-cn.ttf"]
    assert (tmp_app_root / "font" / "ja-jp.ttf").read_bytes() == b"game:ja-jp.ttf"
    assert (tmp_app_root / "font" / "zh-cn.ttf").read_bytes() == b"game:zh-cn.ttf"


def test_only_missing_font_copied(tmp_app_root, game_fonts):
    local_font(tmp_app_root, "ja-jp.ttf")
    fonts = fetch_font()
    assert [font.name for font in fonts] == ["ja-jp.ttf", "zh-cn.ttf"]
    assert (tmp_app_root / "font" / "ja-jp.ttf").read_bytes() == b"local"
    assert (tmp_app_root / "font" / "zh-cn.ttf").read_bytes() == b"game:zh-cn.ttf"


def test_no_game_install_returns_available_subset(tmp_app_root, monkeypatch):
    local_font(tmp_app_root, "ja-jp.ttf")
    monkeypatch.setattr(resources.fonts, "game_font_dir", lambda: None)
    assert [font.name for font in fetch_font()] == ["ja-jp.ttf"]


def test_game_missing_font_returns_available_subset(tmp_app_root, game_fonts):
    (game_fonts / "zh-cn.ttf").unlink()
    assert [font.name for font in fetch_font()] == ["ja-jp.ttf"]


def test_nothing_available_returns_empty(tmp_app_root, monkeypatch):
    monkeypatch.setattr(resources.fonts, "game_font_dir", lambda: None)
    assert fetch_font() == []
