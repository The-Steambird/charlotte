AUDIO_LANGUAGES = {
    "0": ("zh", "中文"),
    "1": ("en", "English"),
    "2": ("ja", "日本語"),
    "3": ("ko", "한국어"),
}

SUBTITLES_LANGUAGES = {
    "CHS": ("zh-Hans", "简体中文"),
    "CHT": ("zh-Hant", "繁體中文"),
    "DE": ("de", "Deutsch"),
    "EN": ("en", "English"),
    "ES": ("es", "Español"),
    "FR": ("fr", "Français"),
    "ID": ("id", "Bahasa Indonesia"),
    "IT": ("it", "Italiano"),
    "JP": ("ja", "日本語"),
    "KR": ("ko", "한국어"),
    "PT": ("pt", "Português"),
    "RU": ("ru", "Русский"),
    "TH": ("th", "ภาษาไทย"),
    "TR": ("tr", "Türkçe"),
    "VI": ("vi", "Tiếng Việt"),
}


def get_language(lang: str) -> str:
    """Translate the subtitle file-name code to a BCP-47 language tag."""
    return SUBTITLES_LANGUAGES.get(lang, ("und", "Unknown"))[0]
