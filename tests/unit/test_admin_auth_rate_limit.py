import time
import pytest
from app.core import auth

def test_admin_auth_brute_force_lockout(client):
    # Reset limiter state before test
    auth.reset_admin_rate_limiter()

    # 5 failed attempts
    for i in range(5):
        resp = client.get("/admin/verify", headers={"Authorization": f"Bearer sk-bad-{i}"})
        assert resp.status_code == 401

    # 6th attempt should be blocked with 429 Too Many Requests
    resp = client.get("/admin/verify", headers={"Authorization": "Bearer sk-bad-6"})
    assert resp.status_code == 429
    assert resp.headers.get("retry-after") is not None
    data = resp.json()
    assert "Too many" in data["detail"]["error"]["message"]

    # Even a valid key is locked out during the ban period
    from app.config import settings
    valid_key = settings.admin_api_key or settings.api_key
    resp = client.get("/admin/verify", headers={"Authorization": f"Bearer {valid_key}"})
    assert resp.status_code == 429

def test_admin_auth_success_resets_counter(client):
    auth.reset_admin_rate_limiter()
    from app.config import settings
    valid_key = settings.admin_api_key or settings.api_key

    # 3 failed attempts
    for i in range(3):
        resp = client.get("/admin/verify", headers={"Authorization": f"Bearer sk-bad-{i}"})
        assert resp.status_code == 401

    # Successful login resets the counter
    resp = client.get("/admin/verify", headers={"Authorization": f"Bearer {valid_key}"})
    assert resp.status_code == 200

    # Can fail 4 more times without being locked out
    for i in range(4):
        resp = client.get("/admin/verify", headers={"Authorization": f"Bearer sk-bad-after-{i}"})
        assert resp.status_code == 401
