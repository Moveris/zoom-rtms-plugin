"""
Shared pytest fixtures for the Moveris Zoom RTMS Plugin test suite.

Fixtures added in later phases:
- mock_moveris_server: local HTTP server (respx) mimicking /api/v1/fast-check-crops
- mock_rtms_signaling_server: local WS server mimicking Zoom RTMS signaling
- mock_rtms_media_server: local WS server sending sample H.264 NAL units
- sample_face_frame: a FaceFrame fixture with a real base64 PNG crop
"""

import pytest


# ---------------------------------------------------------------------------
# Basic settings fixture — overrides env vars for tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def override_settings(monkeypatch):
    """Provide safe dummy credentials so Settings() doesn't fail in tests."""
    monkeypatch.setenv("ZOOM_CLIENT_ID", "test_client_id")
    monkeypatch.setenv("ZOOM_CLIENT_SECRET", "test_client_secret")
    monkeypatch.setenv("ZOOM_WEBHOOK_SECRET_TOKEN", "test_webhook_secret")
    monkeypatch.setenv("MOVERIS_API_KEY", "sk-test-key")
    # Clear lru_cache so each test gets fresh Settings from monkeypatched env
    from src.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
