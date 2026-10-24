import shutil

lazy from pathlib import Path

from resources.game import game_data_dir
from utils.logger import log
from utils.paths import app_root
from utils.strings import translate


def game_font_dir() -> Path | None:
    data_dir = game_data_dir()
    if data_dir is None:
        return None
    font_dir = data_dir / "StreamingAssets" / "MiHoYoSDKRes" / "HttpServerResources" / "font"
    return font_dir if font_dir.is_dir() else None


def fetch_fonts() -> list[Path]:
    font_dir = app_root() / "font"
    fonts = [font_dir / name for name in ("ja-jp.ttf", "zh-cn.ttf")]
    missing = [font for font in fonts if not font.exists()]
    if not missing:
        return fonts

    log.info(translate("FONTS_MISSING"))
    source_dir = game_font_dir()
    if source_dir is not None:
        try:
            font_dir.mkdir(exist_ok=True)
            for font in missing:
                source = source_dir / font.name
                if source.exists():
                    shutil.copy2(source, font)
                    log.info(translate("FONT_CACHED", name=font.name))
        except OSError as e:
            log.warning(translate("FONTS_COPY_FAILED", error=e))

    available = [font for font in fonts if font.exists()]
    if len(available) < len(fonts):
        log.info(
            translate(
                "FONTS_FALLBACK",
                path=r"Genshin Impact\Genshin Impact game\GenshinImpact_Data\StreamingAssets"
                r"\MiHoYoSDKRes\HttpServerResources",
            )
        )
    return available
