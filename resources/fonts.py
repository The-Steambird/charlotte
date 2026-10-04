import shutil
import winreg

from pathlib import Path

from utils.logger import log
from utils.paths import app_root
from utils.strings import translate


def game_font_dir() -> Path | None:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Cognosphere\HYP\1_0\hk4e_global"
        ) as key:
            install_path, _ = winreg.QueryValueEx(key, "GameInstallPath")
    except OSError:
        return None

    font_dir = (
        Path(install_path)
        / "GenshinImpact_Data"
        / "StreamingAssets"
        / "MiHoYoSDKRes"
        / "HttpServerResources"
        / "font"
    )
    return font_dir if font_dir.is_dir() else None


def fetch_font() -> list[Path]:
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
