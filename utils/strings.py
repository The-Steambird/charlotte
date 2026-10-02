import ctypes
import functools
import os
import re

import orjson

from utils.paths import bundle_root


LOCALES_DIR = bundle_root() / "locales"
PLACEHOLDER = re.compile(r"{([a-z][a-z0-9_]*)}")
CHINESE_SCRIPTS = {
    "zh-cn": "zh-hans",
    "zh-sg": "zh-hans",
    "zh-tw": "zh-hant",
    "zh-hk": "zh-hant",
    "zh-mo": "zh-hant",
}


def load(language: str) -> dict[str, str]:
    path = LOCALES_DIR / f"{language}.json"
    return orjson.loads(path.read_bytes())


def lineage(language: str) -> list[str]:
    names = []
    parts = language.lower().split("-")
    while parts:
        names.append("-".join(parts))
        parts.pop()
    if names[0] in CHINESE_SCRIPTS:
        names.insert(1, CHINESE_SCRIPTS[names[0]])
    return names


def closest(requested: str, available: list[str]) -> str | None:
    for wanted in lineage(requested):
        for language in available:
            if wanted in lineage(language):
                return language
    return None


def display_language() -> str:
    buffer = ctypes.create_unicode_buffer(512)
    count = ctypes.c_ulong()
    size = ctypes.c_ulong(len(buffer))
    mui_language_name = 0x8
    get_languages = ctypes.windll.kernel32.GetUserPreferredUILanguages
    get_languages(mui_language_name, ctypes.byref(count), buffer, ctypes.byref(size))
    return buffer.value


@functools.cache
def strings() -> dict[str, str]:
    english = load("en-US")
    requested = os.environ.get("CHARLOTTE_LANG") or display_language()
    available = sorted(path.stem for path in LOCALES_DIR.glob("*.json"))
    language = closest(requested, available)
    if language is None or language == "en-US":
        return english

    translated = load(language)
    return {key: translated.get(key) or english[key] for key in english}


# The placeholders are filled by a regex rather than str.format, because a translation is outside
# text and str.format would follow "{error.__class__}" into the value's attributes.
def translate(key: str, **values: object) -> str:
    def fill(match: re.Match) -> str:
        name = match.group(1)
        if name in values:
            return str(values[name])
        return match.group()

    return PLACEHOLDER.sub(fill, strings()[key])
