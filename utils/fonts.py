import shutil
import sys

from pathlib import Path

from utils.logger import log
from utils.paths import app_root


def fetch_font() -> tuple[Path, Path] | None:
    root = app_root()
    font_ja = root / "font" / "ja-jp.ttf"
    font_zh = root / "font" / "zh-cn.ttf"

    if font_ja.exists() and font_zh.exists():
        return font_ja, font_zh

    log.info("Missing font. Attempting to get font from Genshin Impact installation...")

    install_path = None
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Genshin Impact",
            ) as key:
                install_path, _ = winreg.QueryValueEx(key, "InstallPath")
        except (OSError, ImportError):
            pass

    if not install_path:
        log.info(
            "Subtitles will use the default system font. "
            "To use official fonts, copy the font folder from: "
            r"Genshin Impact\Genshin Impact game\GenshinImpact_Data\StreamingAssets\MiHoYoSDKRes\HttpServerResources"
        )
        return None

    game_font_dir = (
        Path(install_path)
        / "Genshin Impact game"
        / "GenshinImpact_Data"
        / "StreamingAssets"
        / "MiHoYoSDKRes"
        / "HttpServerResources"
        / "font"
    )

    src_ja = game_font_dir / "ja-jp.ttf"
    src_zh = game_font_dir / "zh-cn.ttf"

    if not (game_font_dir.is_dir() and src_ja.exists() and src_zh.exists()):
        log.warning("Failed to fetch fonts from game directory.")
        return None

    try:
        target = root / "font"
        target.mkdir(exist_ok=True)
        shutil.copy2(src_ja, font_ja)
        shutil.copy2(src_zh, font_zh)
        log.info("Fonts cached successfully.")
        return font_ja, font_zh
    except OSError as e:
        log.warning(f"Failed to copy fonts: {e}")
        return None
