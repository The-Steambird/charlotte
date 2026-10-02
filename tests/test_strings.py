import ast

from pathlib import Path

import pytest

from utils.strings import LOCALES_DIR, PLACEHOLDER, closest, load


ROOT = Path(__file__).parent.parent
ENGLISH = load("en-US")


def placeholders(text: str) -> set[str]:
    return set(PLACEHOLDER.findall(text))


def lookups() -> list[ast.Call]:
    sources = list(ROOT.glob("*.py"))
    for folder in ("stages", "resources", "utils", "vs"):
        sources += (ROOT / folder).rglob("*.py")
    return [
        node
        for path in sources
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "translate"
    ]


# The GUI generates a member per key, which makes its build catch a wrong key or argument. Here
# nothing would until that message comes up.
def test_every_lookup_names_an_english_key_with_its_placeholders():
    used = set()
    for call in lookups():
        key = call.args[0]
        assert isinstance(key, ast.Constant), ast.unparse(call)
        assert key.value in ENGLISH, key.value
        assert {keyword.arg for keyword in call.keywords} == placeholders(ENGLISH[key.value]), (
            key.value
        )
        used.add(key.value)

    assert used == set(ENGLISH)


def test_translations_keep_the_english_keys_and_placeholders():
    for path in LOCALES_DIR.glob("*.json"):
        language = path.stem
        for key, text in load(language).items():
            if text:
                assert key in ENGLISH, f"{language}: {key}"
                assert placeholders(text) == placeholders(ENGLISH[key]), f"{language}: {key}"


@pytest.mark.parametrize(
    "requested, expected",
    [
        ("de-AT", "de-DE"),
        ("zh-HK", "zh-TW"),
        ("zh-SG", "zh-CN"),
        ("pt-PT", "pt-BR"),
        ("en-GB", "en-US"),
        ("fr-FR", None),
    ],
)
def test_the_closest_language_is_the_one_the_gui_picks(requested, expected):
    assert closest(requested, ["en-US", "de-DE", "zh-CN", "zh-TW", "pt-BR"]) == expected
