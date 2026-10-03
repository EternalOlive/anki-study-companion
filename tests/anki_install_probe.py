"""Temporary live-Anki installation probe; never included in the release."""
import json
from pathlib import Path
import sys
import traceback

from aqt import gui_hooks
from aqt.qt import QApplication, QTimer

OUT = Path(r"C:\Users\yoon\Documents\Codex\anki\artifacts")


def inspect_installation():
    try:
        module = sys.modules.get("study_companion.addon")
        assert module is not None, "Study add-on not loaded"
        control = module.controller
        assert control is not None, "Controller not initialized"
        panel = control.panel_body
        assert panel.isVisible(), "Panel hidden"
        original = control.locale
        for locale in ("ko", "en"):
            control.locale = locale
            control.refresh()
            QApplication.processEvents()
            panel.layout().activate()
            assert panel.grab().save(str(OUT / f"anki-installed-{locale}.png"))
        control.locale = original
        control.refresh()
        result = {
            "ok": True,
            "panel": type(panel).__name__,
            "locale": original,
            "display_code": control.online.get("display_name"),
            "group": bool(control.online.get("group")),
            "sync_error": bool(control.online.get("last_error")),
            "friend_count": len(panel.member_rows),
        }
    except Exception:
        result = {"ok": False, "traceback": traceback.format_exc()}
    OUT.mkdir(exist_ok=True)
    (OUT / "anki-install-check.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


gui_hooks.profile_did_open.append(lambda: QTimer.singleShot(15000, inspect_installation))
