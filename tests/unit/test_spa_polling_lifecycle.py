"""Static wiring guards for SPA polling ownership and interval cleanup."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/app/app.js").read_text(encoding="utf-8")
LOGS = (ROOT / "static/app/logs.js").read_text(encoding="utf-8")
BABYSIT = (ROOT / "static/app/babysit.js").read_text(encoding="utf-8")


def test_navigation_activates_only_the_visible_sections_pollers():
    assert "setLogsPollingActive(sectionId === 'logs')" in APP
    assert "setBabysitPollingActive(sectionId === 'babysit')" in APP
    assert "stopDashboardAutoRefresh();" in APP
    assert "stopUptimeTimer();" in APP


def test_each_interval_has_an_idempotent_clear_interval_path():
    assert "export function setLogsPollingActive(active)" in LOGS
    assert "startPolling() {" in LOGS and "stopPolling();" in LOGS
    assert "clearInterval(pollTimer);" in LOGS
    assert "pollTimer = null;" in LOGS

    assert "export function setBabysitPollingActive(active)" in BABYSIT
    assert "isSectionActive = Boolean(active);" in BABYSIT
    assert "if (isSectionActive && isAutoPollingEnabled)" in BABYSIT
    assert "startPolling() {" in BABYSIT and "stopPolling();" in BABYSIT
    assert "clearInterval(pollTimer);" in BABYSIT
    assert "pollTimer = null;" in BABYSIT

    assert "clearInterval(_dashboardRefreshTimer);" in APP
    assert "_dashboardRefreshTimer = null;" in APP
    assert "clearInterval(uptimeInterval);" in APP
    assert "uptimeInterval = null;" in APP


def test_hidden_pollers_are_not_started_unconditionally_at_app_boot():
    # Initialization binds controls. activateSection() is the sole polling owner.
    assert "export function initLogs() {\n    initLogControls();\n}" in LOGS
    assert "export function initBabysit() {\n    initControls();\n}" in BABYSIT
