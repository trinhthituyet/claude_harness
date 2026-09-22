"""Which `claude` binary sessions run, and why the default matters."""

from __future__ import annotations

import importlib
import shutil


def reload_settings():
    import app.config

    importlib.reload(app.config)
    return app.config.settings


def test_defaults_to_the_claude_on_path(monkeypatch):
    """The user's own install carries their auth and proxy config; the bundled one
    does not, and when it cannot reach the API it hangs rather than failing."""
    monkeypatch.delenv("HARNESS_CLI_PATH", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/claude")
    assert reload_settings().cli_path == "/usr/local/bin/claude"


def test_falls_back_to_the_bundled_cli_when_none_is_installed(monkeypatch):
    monkeypatch.delenv("HARNESS_CLI_PATH", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert reload_settings().cli_path is None


def test_explicit_override_wins(monkeypatch):
    monkeypatch.setenv("HARNESS_CLI_PATH", "/opt/custom/claude")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/claude")
    assert reload_settings().cli_path == "/opt/custom/claude"


def test_bundled_can_be_forced(monkeypatch):
    monkeypatch.setenv("HARNESS_CLI_PATH", "bundled")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/claude")
    assert reload_settings().cli_path is None
