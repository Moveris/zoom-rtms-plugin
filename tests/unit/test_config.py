"""Unit tests for Settings configuration loading."""

import pytest

from src.config import get_settings


def test_settings_load_from_env(monkeypatch):
    monkeypatch.setenv("ZOOM_CLIENT_ID", "my_client_id")
    monkeypatch.setenv("ZOOM_CLIENT_SECRET", "my_secret")
    monkeypatch.setenv("ZOOM_WEBHOOK_SECRET_TOKEN", "my_webhook_token")
    monkeypatch.setenv("MOVERIS_API_KEY", "sk-live-abc123")
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.zoom_client_id == "my_client_id"
    assert settings.zoom_client_secret == "my_secret"
    assert settings.moveris_api_key == "sk-live-abc123"


def test_default_liveness_threshold():
    settings = get_settings()
    # Per Moveris API docs: score >= 65 = live
    assert settings.liveness_threshold == 65


def test_default_moveris_mode():
    settings = get_settings()
    assert settings.moveris_mode == "fast"


def test_default_frame_sample_rate():
    settings = get_settings()
    assert settings.frame_sample_rate == 5


def test_default_max_concurrent_sessions():
    settings = get_settings()
    assert settings.max_concurrent_sessions == 50


def test_invalid_moveris_mode_raises(monkeypatch):
    monkeypatch.setenv("MOVERIS_MODE", "invalid_mode")
    get_settings.cache_clear()
    with pytest.raises(Exception):
        get_settings()
    get_settings.cache_clear()
