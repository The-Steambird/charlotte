AUDIO_LANGUAGES = {
    "0": "zh",
    "1": "en",
    "2": "ja",
    "3": "ko",
}

SUBTITLES_LANGUAGES = {
    "CHS": ("chi-CN", "简体中文"),
    "CHT": ("chi-TW", "繁體中文"),
    "DE": ("ger", "Deutsch"),
    "EN": ("eng", "English"),
    "ES": ("spa", "Español"),
    "FR": ("fre", "Français"),
    "ID": ("ind", "Bahasa Indonesia"),
    "IT": ("ita", "Italiano"),
    "JP": ("jpn", "日本語"),
    "KR": ("kor", "한국어"),
    "PT": ("por", "Português"),
    "RU": ("rus", "Русский"),
    "TH": ("tha", "ภาษาไทย"),
    "TR": ("tur", "Türkçe"),
    "VI": ("vie", "Tiếng Việt"),
}

DICTIONARY = {
    "chi": "zh",
    "ger": "de",
    "eng": "en",
    "spa": "es",
    "fre": "fr",
    "ind": "id",
    "ita": "it",
    "jpn": "ja",
    "kor": "ko",
    "por": "pt",
    "rus": "ru",
    "tha": "th",
    "tur": "tr",
    "vie": "vi",
    "und": "und",
    "chi-CN": "zh",
    "chi-TW": "zh",
}


def get_language(lang: str) -> str:
    """Translate subtitles language code from file name to ISO 639-1."""
    code, lang = SUBTITLES_LANGUAGES.get(lang, ("und", "Unknown"))
    return DICTIONARY.get(code, "und")
