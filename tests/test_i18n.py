import re

import pytest

from job_finder.i18n import COMMANDS, MESSAGES, command, command_aliases, language_name, t


def test_every_language_has_the_same_keys() -> None:
    for lang, messages in MESSAGES.items():
        assert messages.keys() == MESSAGES["en"].keys(), lang


@pytest.mark.parametrize("lang", sorted(MESSAGES))
def test_translations_use_the_same_placeholders(lang: str) -> None:
    def placeholders(text: str) -> set[str]:
        return set(re.findall(r"{(\w+)}", text))

    for key, text in MESSAGES["en"].items():
        assert placeholders(MESSAGES[lang][key]) == placeholders(text), key


def test_unknown_language_falls_back_to_english() -> None:
    assert t("xx", "digest.none") == MESSAGES["en"]["digest.none"]
    assert language_name("xx") == "English"
    assert language_name("pt-BR") == "Brazilian Portuguese"


def test_commands_and_aliases() -> None:
    assert command("search", "pt-BR") == "buscar"
    assert command("search", "en") == "search"
    assert command("status", "pt-BR") == "status"
    assert command_aliases("evaluate") == ["evaluate", "avaliar"]
    assert command_aliases("status") == ["status"]
    # Telegram commands: lowercase letters, digits and underscores, up to 32 chars.
    for name in COMMANDS:
        for alias in command_aliases(name):
            assert re.fullmatch(r"[a-z0-9_]{1,32}", alias)
