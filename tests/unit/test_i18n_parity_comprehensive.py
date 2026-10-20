"""Comprehensive key-set parity gate for every web-console locale.

Feature-specific tests historically checked only their own prefixes, which allowed unrelated
translations to drift.  This test treats the complete locale dictionaries as one schema.
"""

import re
from pathlib import Path


I18N_PATH = Path(__file__).resolve().parents[2] / "static" / "app" / "i18n.js"
EXPECTED_LOCALES = ("zh-CN", "en-US", "ja-JP", "ko-KR", "zh-TW")
LOCALE_HEADER = re.compile(r"^    '([^']+)': \{$", re.MULTILINE)
KEY_LINE = re.compile(r"^        '([^']+)':", re.MULTILINE)


def _locale_keys(source: str) -> dict[str, list[str]]:
    headers = list(LOCALE_HEADER.finditer(source))
    blocks: dict[str, list[str]] = {}
    object_end = source.index("\n};", headers[-1].end()) if headers else 0
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else object_end
        blocks[header.group(1)] = KEY_LINE.findall(source[header.end():end])
    return blocks


def test_all_five_i18n_locales_have_identical_complete_key_sets():
    blocks = _locale_keys(I18N_PATH.read_text(encoding="utf-8"))

    assert tuple(blocks) == EXPECTED_LOCALES, (
        f"expected locale order {EXPECTED_LOCALES}, found {tuple(blocks)}"
    )

    # Duplicate object keys are silently overwritten by JavaScript and can hide a missing
    # translation, so parity alone is not enough: each locale must also be internally unique.
    for locale, keys in blocks.items():
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        assert not duplicates, f"{locale} contains duplicate translation keys: {duplicates}"
        assert keys, f"{locale} unexpectedly contains no translation keys"

    baseline = set(blocks["en-US"])
    mismatches = {}
    for locale, keys in blocks.items():
        current = set(keys)
        missing = sorted(baseline - current)
        extra = sorted(current - baseline)
        if missing or extra:
            mismatches[locale] = {"missing": missing, "extra": extra}

    assert not mismatches, f"i18n locale key sets differ from en-US: {mismatches}"
