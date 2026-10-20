"""Configuration readiness: effective values, sources, restart markers and drift.

The readiness view reuses the existing settings schema; it must never render a
secret and it must tell the operator when a persisted override is invalid or
shadowed by the environment.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_I18N_JS = _ROOT / "static" / "app" / "i18n.js"
_SETTINGS_HTML = _ROOT / "static" / "components" / "section-settings.html"
_LANGS = ("zh-CN", "en-US", "ja-JP", "ko-KR", "zh-TW")


class _FakeSettings:
    def __init__(self, **values):
        self.api_key = values.pop("api_key", "sk-test-key")
        self.admin_api_key = values.pop("admin_api_key", "")
        self.gemini_psid = values.pop("gemini_psid", "")
        self.gemini_psidts = values.pop("gemini_psidts", "")
        self.argon_system_prompt = values.pop("argon_system_prompt", "")
        self.argon_timeout = values.pop("argon_timeout", 360)
        self.usage_stats_enabled = values.pop("usage_stats_enabled", True)
        self.chat_cleanup_enabled = values.pop("chat_cleanup_enabled", True)
        self.argon_fallback_to_flash = values.pop("argon_fallback_to_flash", True)
        self.fallback_enabled = values.pop("fallback_enabled", False)
        self.health_check_enabled = values.pop("health_check_enabled", True)
        self.log_bodies_enabled = values.pop("log_bodies_enabled", False)
        self.docs_enabled = values.pop("docs_enabled", False)
        self.rotation_strategy = values.pop("rotation_strategy", "round-robin")
        for key, value in values.items():
            setattr(self, key, value)


def _report(**kwargs):
    from app.core.config_diagnostics import build_config_report

    settings_obj = kwargs.pop("settings_obj", None) or _FakeSettings()
    return build_config_report(settings_obj, **kwargs)


def test_effective_value_and_source_are_reported():
    report = _report(env={"ARGON_TIMEOUT": "120"}, overrides={})
    field = report["fields"]["argon_timeout"]
    assert field["value"] == 360
    assert field["source"] == "env"
    assert field["env"] == 120


def test_a_persisted_override_wins_over_the_environment():
    # The startup replay (app.config.apply_overrides) has already applied the
    # override to the settings object, so the effective value is the override.
    report = _report(settings_obj=_FakeSettings(argon_timeout=90),
                     env={"ARGON_TIMEOUT": "120"}, overrides={"argon_timeout": 90})
    field = report["fields"]["argon_timeout"]
    assert field["source"] == "override"
    assert field["value"] == 90
    assert field["env"] == 120
    assert field["override"] == 90
    assert any(item["field"] == "argon_timeout" and item["kind"] == "env_overridden"
               for item in report["drift"])


def test_an_override_that_did_not_take_effect_is_reported_as_drift():
    report = _report(settings_obj=_FakeSettings(argon_timeout=360),
                     env={}, overrides={"argon_timeout": 90})
    assert any(item["field"] == "argon_timeout" and item["kind"] == "override_not_applied"
               for item in report["drift"])


def test_default_source_when_nothing_overrides_the_value():
    report = _report(env={}, overrides={})
    assert report["fields"]["argon_timeout"]["source"] == "default"
    assert report["fields"]["argon_timeout"].get("env") is None


def test_restart_required_settings_are_marked():
    report = _report(env={}, overrides={})
    restart = report["restart_required"]
    assert "usage_stats_enabled" in restart
    assert "chat_cleanup_enabled" in restart
    assert "argon_timeout" not in restart


def test_invalid_override_is_reported_as_an_issue():
    report = _report(env={}, overrides={"argon_timeout": "not-a-number"})
    assert report["validation"]["ok"] is False
    assert any(issue["field"] == "argon_timeout" for issue in report["validation"]["issues"])
    assert any(item["kind"] == "invalid_override" for item in report["drift"])


def test_out_of_domain_override_is_reported():
    report = _report(env={}, overrides={"rotation_strategy": "garbage"})
    assert report["validation"]["ok"] is False
    assert any(issue["field"] == "rotation_strategy" for issue in report["validation"]["issues"])


def test_secrets_are_never_rendered():
    settings_obj = _FakeSettings(api_key="sk-super-secret", admin_api_key="sk-admin-secret",
                                  gemini_psid="psid-value", argon_system_prompt="secret prompt text")
    report = _report(settings_obj=settings_obj, env={}, overrides={})
    serialized = str(report)
    assert "sk-super-secret" not in serialized
    assert "sk-admin-secret" not in serialized
    assert "psid-value" not in serialized
    assert "secret prompt text" not in serialized
    assert report["secrets"]["api_key"]["configured"] is True
    assert report["secrets"]["admin_api_key"]["configured"] is True
    assert report["secrets"]["argon_system_prompt"]["length"] == len("secret prompt text")


def test_feature_switches_are_reported():
    report = _report(settings_obj=_FakeSettings(usage_stats_enabled=False, health_check_enabled=False),
                     env={}, overrides={})
    features = report["features"]
    assert features["usage_stats"] is False
    assert features["health_checks"] is False
    assert features["diagnostics"] is True
    assert features["argon_fallback"] is True
    assert features["thirdparty_fallback"] is False


def test_admin_config_endpoint_is_protected_and_redacted(client):
    response = client.get("/admin/config")
    assert response.status_code == 401
    response = client.get("/admin/config", headers={"Authorization": "Bearer sk-test-key"})
    assert response.status_code == 200
    body = response.json()
    assert "fields" in body and "secrets" in body and "features" in body
    assert "sk-test-key" not in response.text
    assert "psid" not in response.text.lower() or "gemini_psid" in response.text


# --------------------------------------------------------------------------------------
# i18n and markup
# --------------------------------------------------------------------------------------

def _locale_keys(prefix: str) -> dict[str, set[str]]:
    blocks: dict[str, set[str]] = {}
    current = None
    for line in _I18N_JS.read_text(encoding="utf-8").splitlines():
        lang = re.match(r"^ {4}'([A-Za-z-]+)': \{$", line)
        if lang:
            current = lang.group(1)
            blocks[current] = set()
            continue
        if current is None:
            continue
        key = re.match(rf"^ +'({re.escape(prefix)}[^']*)':", line)
        if key:
            blocks[current].add(key.group(1))
    return blocks


def test_config_keys_exist_in_every_language():
    blocks = _locale_keys("dashboard.config")
    assert set(_LANGS) <= set(blocks), sorted(blocks)
    needed = {
        "dashboard.configTitle", "dashboard.configPrivacy", "dashboard.configEffective",
        "dashboard.configField", "dashboard.configValue", "dashboard.configSource",
        "dashboard.configSourceEnv", "dashboard.configSourceOverride", "dashboard.configSourceDefault",
        "dashboard.configRestart", "dashboard.configRestartRequired", "dashboard.configFeatures",
        "dashboard.configSecrets", "dashboard.configConfigured", "dashboard.configNotConfigured",
        "dashboard.configEnabled", "dashboard.configDisabled", "dashboard.configValidation",
        "dashboard.configNoIssues", "dashboard.configNoData",
    }
    missing = {lang: sorted(needed - blocks[lang]) for lang in _LANGS if needed - blocks[lang]}
    assert not missing, missing


def test_all_language_blocks_share_the_same_config_keys():
    blocks = _locale_keys("dashboard.config")
    baseline = blocks["zh-CN"]
    assert baseline, "no dashboard.config* keys parsed - i18n.js format changed?"
    drift = {
        lang: {"missing": sorted(baseline - blocks[lang]), "extra": sorted(blocks[lang] - baseline)}
        for lang in _LANGS
        if blocks[lang] != baseline
    }
    assert not drift, drift


def test_settings_markup_contains_the_config_panel():
    html = _SETTINGS_HTML.read_text(encoding="utf-8")
    for element_id in ("config-state", "config-rows", "config-features", "config-secrets", "config-issues"):
        assert f'id="{element_id}"' in html, element_id
    assert 'aria-live="polite"' in html


# --------------------------------------------------------------------------------------
# Frontend rendering (node)
# --------------------------------------------------------------------------------------

import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import textwrap  # noqa: E402

_NODE = shutil.which("node")
_needs_node = pytest.mark.skipif(_NODE is None, reason="node is not available")
_APP_JS = _ROOT / "static" / "app" / "app.js"

_PRELUDE = textwrap.dedent("""
    const _ARGV = JSON.parse(process.argv[2]);
    function t(k) { return 'T:' + k; }
    function escapeHtml(text) {
        return String(text == null ? '' : text)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
    function formatNumber(num) { return String(num == null ? 0 : num); }
    function makeEl() {
        return { id: '', className: '', textContent: '', innerHTML: '', value: '',
                 _attrs: {}, listeners: {},
                 setAttribute(k, v) { this._attrs[k] = v; },
                 addEventListener(ev, fn) { this.listeners[ev] = fn; },
                 querySelectorAll() { return []; } };
    }
    const _els = {};
    const document = { getElementById(id) { return _els[id] || null; } };
""")


def _extract_function(source: str, name: str) -> str:
    head = source.index(f"function {name}(")
    if source[max(0, head - 6):head] == "async ":
        head -= 6
    depth = 0
    i = source.index("{", head)
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[head:i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces for {name}")


@_needs_node
def test_render_config_readiness_marks_sources_and_restarts(tmp_path):
    src = _APP_JS.read_text(encoding="utf-8")
    functions = [
        _extract_function(src, name)
        for name in ("sloStateDescriptor", "formatRatio", "formatDurationMs",
                     "renderSloMetrics", "renderConfigReadiness")
    ]
    script = "\n".join([_PRELUDE, *functions, textwrap.dedent("""
        (async () => {
            for (const id of ['config-state', 'config-rows', 'config-features', 'config-secrets', 'config-issues']) {
                _els[id] = makeEl();
            }
            renderConfigReadiness({
                fields: {
                    argon_timeout: { value: 360, source: 'env', env: 120, restart_required: false },
                    usage_stats_enabled: { value: true, source: 'override', restart_required: true },
                },
                secrets: { api_key: { configured: true, source: 'env' } },
                features: { diagnostics: true, usage_stats: false },
                validation: { ok: false, issues: [{ field: 'argon_timeout', issue: 'wrong type' }] },
                drift: [{ field: 'argon_timeout', kind: 'env_overridden', detail: 'override wins' }],
            });
            process.stdout.write(JSON.stringify({
                chipClass: _els['config-state'].className,
                chipText: _els['config-state'].textContent,
                rows: _els['config-rows'].innerHTML,
                features: _els['config-features'].innerHTML,
                secrets: _els['config-secrets'].innerHTML,
                issues: _els['config-issues'].innerHTML,
            }));
        })();
    """)])
    path = tmp_path / "config.mjs"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run([_NODE, str(path), json.dumps({})], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["chipClass"] == "slo-state slo-state--degraded"
    assert out["chipText"] == "T:dashboard.sloStateDegraded"
    assert "T:dashboard.configSourceEnv" in out["rows"]
    assert "T:dashboard.configSourceOverride" in out["rows"]
    assert "T:dashboard.configRestartRequired" in out["rows"]
    assert "T:dashboard.configDisabled" in out["features"]
    assert "T:dashboard.configConfigured" in out["secrets"]
    assert "wrong type" in out["issues"] and "override wins" in out["issues"]
