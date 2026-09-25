import shutil
import sys

from pathlib import Path

from utils.logger import log
from utils.paths import app_root


def game_font_dir() -> Path | None:
    if sys.platform != "win32":
        return None

    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Genshin Impact",
        ) as key:
            install_path, _ = winreg.QueryValueEx(key, "InstallPath")
    except OSError, ImportError:
        return None

    font_dir = (
        Path(install_path)
        / "Genshin Impact game"
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

    log.info("Missing font. Attempting to get font from Genshin Impact installation...")
    source_dir = game_font_dir()
    if source_dir is not None:
        try:
            font_dir.mkdir(exist_ok=True)
            for font in missing:
                source = source_dir / font.name
                if source.exists():
                    shutil.copy2(source, font)
                    log.info(f"Cached {font.name} from game installation.")
        except OSError as e:
            log.warning(f"Failed to copy fonts: {e}")

    available = [font for font in fonts if font.exists()]
    if len(available) < len(fonts):
        log.info(
            "Subtitles will use the default system font. "
            "To use official fonts, copy the font folder from: "
            "Genshin Impact"
            r"\Genshin Impact game"
            r"\GenshinImpact_Data"
            r"\StreamingAssets"
            r"\MiHoYoSDKRes"
            r"\HttpServerResources"
        )
    return available
