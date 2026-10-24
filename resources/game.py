import winreg

from pathlib import Path


def game_data_dir() -> Path | None:
    """The GenshinImpact_Data folder of the install the launcher registered."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Cognosphere\HYP\1_0\hk4e_global"
        ) as key:
            install_path, _ = winreg.QueryValueEx(key, "GameInstallPath")
    except OSError:
        return None

    data_dir = Path(install_path) / "GenshinImpact_Data"
    return data_dir if data_dir.is_dir() else None


def game_video_dir() -> Path | None:
    data_dir = game_data_dir()
    if data_dir is None:
        return None
    video_dir = data_dir / "StreamingAssets" / "VideoAssets" / "StandaloneWindows64"
    return video_dir if video_dir.is_dir() else None
