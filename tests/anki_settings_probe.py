"""Temporary live-Anki check, excluded from the distributable add-on."""
import json
from pathlib import Path
import sys
import traceback

from aqt import gui_hooks, mw
from aqt.qt import QApplication, QSpinBox, QTimer

OUT = Path(r"C:\Users\yoon\Documents\Codex\anki\artifacts")


def inspect_settings():
    result = {}
    control = None
    original_locale = None
    dialog = None
    try:
        from study_companion.settings import SettingsDialog
        control = sys.modules["study_companion.addon"].controller
        original_locale = control.locale
        before = (control.tracker.time_goal_minutes, control.tracker.card_goal)
        OUT.mkdir(exist_ok=True)
        for locale in ("ko", "en"):
            control.locale = locale
            dialog = SettingsDialog(control, mw)
            dialog.show()
            QApplication.processEvents()
            assert dialog.grab().save(str(OUT / f"anki-settings-{locale}.png"))
            inputs = dialog.findChildren(QSpinBox)
            assert len(inputs) == 2
            inputs[0].setValue(123)
            dialog.reject()
            QApplication.processEvents()
            assert before == (control.tracker.time_goal_minutes, control.tracker.card_goal)
            dialog.deleteLater()
            dialog = None
        result = {"ok": True, "cancel_preserves_goals": True,
                  "locales": ["ko", "en"], "sync_error": bool(control.online.get("last_error"))}
    except Exception:
        result = {"ok": False, "traceback": traceback.format_exc()}
    finally:
        if dialog is not None:
            dialog.reject()
            dialog.deleteLater()
        if control is not None and original_locale is not None:
            control.locale = original_locale
            control.refresh()
    (OUT / "anki-settings-check.json").write_text(json.dumps(result), encoding="utf-8")
    if result.get("ok"):
        QTimer.singleShot(0, control.show_dialog)


gui_hooks.profile_did_open.append(lambda: QTimer.singleShot(12000, inspect_settings))
