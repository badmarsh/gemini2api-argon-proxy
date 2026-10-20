"""Regression guards for the four-theme dashboard polish and light-theme contrast."""

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
BASE = (ROOT / "static/app/base.css").read_text(encoding="utf-8")
DASHBOARD = (ROOT / "static/components/section-dashboard.css").read_text(encoding="utf-8")
LOGS = (ROOT / "static/components/section-logs.css").read_text(encoding="utf-8")
ACCOUNTS = (ROOT / "static/components/section-accounts.css").read_text(encoding="utf-8")


def _theme_vars(selector: str) -> dict[str, str]:
    start = BASE.index(selector)
    body_start = BASE.index("{", start) + 1
    body_end = BASE.index("}\n", body_start)
    return dict(re.findall(r"--([\w-]+):\s*([^;]+);", BASE[body_start:body_end]))


def _rgb(value: str) -> tuple[float, float, float]:
    value = value.strip()
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", value), value
    return tuple(int(value[index:index + 2], 16) / 255 for index in (1, 3, 5))


def _luminance(value: str) -> float:
    def linear(channel: float) -> float:
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
    red, green, blue = (linear(channel) for channel in _rgb(value))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(left: str, right: str) -> float:
    bright, dark = sorted((_luminance(left), _luminance(right)), reverse=True)
    return (bright + 0.05) / (dark + 0.05)


@pytest.mark.parametrize("selector", ['[data-theme="vercel-light"]', '[data-theme="supabase-light"]'])
def test_light_theme_strong_borders_and_code_text_reach_aa_contrast(selector):
    variables = _theme_vars(selector)
    background = variables["bg-primary"]
    assert _contrast(variables["border-strong"], background) >= 4.5
    assert _contrast(variables["code-text"], variables["code-bg"]) >= 4.5

    # Badge text colors are explicit rather than inherited from a mismatched theme.
    for name in ("success-text", "warning-text", "danger-text", "info-text"):
        assert _contrast(variables[name], background) >= 4.5, f"{selector} {name} is washed out"


def test_quick_integration_tabs_have_equal_geometry_and_bounded_code_panels():
    assert "grid-template-columns: repeat(3, minmax(0, 1fr));" in DASHBOARD
    assert ".quick-endpoint-snippet[hidden]" in DASHBOARD
    assert "min-height: 21rem;" in DASHBOARD
    assert "height: 100%;" in DASHBOARD
    assert "overflow: auto;" in DASHBOARD
    assert "var(--border-strong" in DASHBOARD


def test_logs_table_has_sticky_aligned_header_and_bounded_smooth_scrolling():
    assert "max-height: min(70vh, 46rem);" in LOGS
    assert "scrollbar-gutter: stable both-edges;" in LOGS
    assert "scroll-behavior: smooth;" in LOGS
    assert "table-layout: fixed;" in LOGS
    assert "position: sticky;" in LOGS
    assert "font-variant-numeric: tabular-nums;" in LOGS
    assert "border: 1px solid var(--border-strong" in LOGS
    assert ".status-2xx { color: var(--success-text" in LOGS
    assert ".status-5xx { color: var(--danger-text" in LOGS
    assert "color: var(--info-text" in LOGS


def test_quota_badges_wrap_on_cards_and_stack_on_narrow_screens():
    assert ".account-quota-badge" in ACCOUNTS
    assert "white-space: normal;" in ACCOUNTS
    assert "@media (max-width: 420px)" in ACCOUNTS
    assert "flex-direction: column;" in ACCOUNTS
