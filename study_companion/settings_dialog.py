"""Settings dialog for the study companion."""

from __future__ import annotations

try:
    from .settings import SettingsDialog
except ModuleNotFoundError:
    # Inside headless test environments without aqt/PyQt6 installed
    SettingsDialog = None

__all__ = ["SettingsDialog"]
