"""Tests for environment loading and narrative helpers."""

import os

from app import narrative


def test_load_environment_reads_app_dotenv(tmp_path, monkeypatch):
    """The app should load API keys from the app-local .env file when present."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / ".env").write_text("GEMINI_API_KEY=test-key\n")

    monkeypatch.setattr(narrative, "__file__", str(app_dir / "narrative.py"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    narrative.load_environment()

    assert os.getenv("GEMINI_API_KEY") == "test-key"
