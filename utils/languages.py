AUDIO_LANGUAGES = {
    "0": "zh",
    "1": "en",
    "2": "ja",
    "3": "ko",
}

SUBTITLES_LANGUAGES = {
    "CHS": ("zh", "简体中文"),
    "CHT": ("zh", "繁體中文"),
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
    """Translate subtitles language code from file name to ISO 639-1."""
    return SUBTITLES_LANGUAGES.get(lang, ("und", "Unknown"))[0]
