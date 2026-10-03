"""Render the native study panel with Anki's bundled PyQt6.

This is intentionally a standalone smoke script instead of a unittest: the
normal test suite remains Anki-independent, while this script exercises the
real widget tree and writes inspectable PNG artifacts.
"""

from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ANKI_PACKAGES = Path(
    os.environ.get(
        "ANKI_APP_PACKAGES",
        r"C:\Users\yoon\AppData\Local\Programs\Anki\app_packages",
    )
)
OUTPUT = ROOT / "artifacts"


def _load_panel_types():
    if not ANKI_PACKAGES.exists():
        raise SystemExit(
            "Anki app_packages not found. Set ANKI_APP_PACKAGES to its path."
        )

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(ANKI_PACKAGES))

    from PyQt6 import QtCore, QtGui, QtWidgets

    # Import panel.py through the package path without executing the add-on's
    # integration __init__.py, which expects a running Anki main window.
    aqt = types.ModuleType("aqt")
    qt = types.ModuleType("aqt.qt")
    for name in (
        "QApplication",
        "QButtonGroup",
        "QFrame",
        "QGridLayout",
        "QHBoxLayout",
        "QLabel",
        "QPushButton",
        "QScrollArea",
        "QSizePolicy",
        "QStyle",
        "QToolButton",
        "QVBoxLayout",
        "QWidget",
    ):
        setattr(qt, name, getattr(QtWidgets, name))
    qt.Qt = QtCore.Qt
    for name in ("QPainter", "QPalette", "QPen"):
        setattr(qt, name, getattr(QtGui, name))
    sys.modules["aqt"] = aqt
    sys.modules["aqt.qt"] = qt

    package = types.ModuleType("study_companion")
    package.__path__ = [str(ROOT / "study_companion")]
    sys.modules["study_companion"] = package

    from study_companion.panel import StudyPanel
    from study_companion.tracker import StudyTracker, TIMEZONE

    return QtGui, QtWidgets, StudyPanel, StudyTracker, TIMEZONE


class FakeController:
    def __init__(self, tracker, current):
        self.tracker = tracker
        self.locale = "ko"
        self.online = {
            "group": {"id": "group-1", "name": "goyori"},
            "auth": {"user_id": "self"},
            "members": [
                {
                    "user_id": "friend-a",
                    "display_name": "K7M-2RX",
                    "status": "studying",
                    "updated_at": current.isoformat(),
                    "active_seconds": 25 * 60,
                    "answer_count": 42,
                    "time_goal_minutes": 60,
                    "card_goal": 100,
                },
                {
                    "user_id": "friend-b",
                    "display_name": "T4N-8WA",
                    "status": "stopped",
                    "updated_at": (current - timedelta(seconds=30)).isoformat(),
                    "active_seconds": 12 * 60,
                    "answer_count": 19,
                    "time_goal_minutes": 45,
                    "card_goal": 80,
                },
            ],
        }

    def t(self, korean, english):
        return english if self.locale == "en" else korean

    def _current_member_status(self, member):
        return "online" if member.get("status") == "stopped" else member.get("status")

    def display_member_name(self, member):
        return member.get("display_name", "Friend")

    def show_dialog(self):
        pass

    def sync_async(self, force=False):
        pass


def main() -> int:
    QtGui, QtWidgets, StudyPanel, StudyTracker, timezone = _load_panel_types()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setStyle("Fusion")
    font_path = os.environ.get("PANEL_SMOKE_FONT", r"C:\Windows\Fonts\malgun.ttf")
    font_id = QtGui.QFontDatabase.addApplicationFont(font_path)
    if font_id < 0:
        raise SystemExit(
            "Smoke font could not be loaded. Set PANEL_SMOKE_FONT to a TTF path."
        )
    family = QtGui.QFontDatabase.applicationFontFamilies(font_id)[0]
    app.setFont(QtGui.QFont(family, 9))

    current = datetime.now(timezone).replace(microsecond=0)
    today = current.date().isoformat()
    yesterday = (current.date() - timedelta(days=1)).isoformat()
    tracker = StudyTracker(
        records={today: {"seconds": 24 * 60 + 10, "answers": 48}},
        deck_records={
            today: {
                "10": {
                    "name": "Biology",
                    "seconds": 20 * 60,
                    "answers": 40,
                }
            },
            yesterday: {
                "10": {
                    "name": "Biology",
                    "seconds": 30 * 60,
                    "answers": 45,
                }
            },
        },
        time_goal_minutes=60,
        card_goal=100,
    )
    tracker.current_deck_id = "10"
    tracker.current_deck_name = "Biology"
    tracker.status = "studying"

    controller = FakeController(tracker, current)
    panel = StudyPanel(controller)
    panel.resize(320, 720)
    panel.show()
    app.processEvents()

    first_key = panel.member_order[0]
    first_row = panel.member_rows[first_key]
    friend = first_row.member
    friend["current_deck_name"] = "영어::<단어>"
    friend["deck_updated_at"] = current.isoformat()
    panel.refresh()
    assert "현재 덱  영어::<단어>" in first_row.details.text()
    friend["deck_updated_at"] = (current - timedelta(seconds=100)).isoformat()
    panel.refresh()
    assert "<단어>" not in first_row.details.text()
    friend["deck_updated_at"] = current.isoformat()
    friend["status"] = "paused"
    panel.refresh()
    assert "공부 중 아님" in first_row.details.text()
    friend["status"] = "studying"
    panel.refresh()
    first_row.identity.click()
    panel.history_toggle.setChecked(True)
    app.processEvents()

    assert panel.width() == 320
    assert first_row.details.isVisible()
    assert panel.history_body.isVisible()
    assert len(panel.member_rows) == 2
    assert first_row.dot.text() == "●" and first_row.dot.isVisible()
    online_row = panel.member_rows["friend-b"]
    assert online_row.dot.text() == "○" and online_row.dot.isVisible()
    assert online_row.dot.styleSheet() == ""
    assert panel.own_time.text() == "24:10 / 60분"
    assert panel.collapse_panel.toolTip() == "패널 접기"
    assert panel.format_duration(3600) == "1시간 00분"
    assert panel.format_clock(3661) == "1시간 01분"
    assert panel.format_clock(3599) == "59:59"
    panel.set_collapsed(True)
    app.processEvents()
    assert panel.collapsed
    assert not panel.content.isVisible()
    assert panel.expand_panel.isVisible()
    assert panel.maximumWidth() == 52
    panel.set_collapsed(False)
    app.processEvents()
    assert panel.content.isVisible()
    assert not panel.expand_panel.isVisible()
    assert panel.minimumWidth() == 280

    OUTPUT.mkdir(exist_ok=True)
    korean = OUTPUT / "native-ko.png"
    if not panel.grab().save(str(korean)):
        raise RuntimeError(f"could not save {korean}")

    original_order = list(panel.member_order)
    controller.online["members"].reverse()
    controller.locale = "en"
    panel.refresh()
    app.processEvents()

    assert panel.member_order == original_order
    assert first_row.expanded and first_row.details.isVisible()
    assert panel.own_title.text() == "This PC · today"
    assert "Current deck  영어::<단어>" in first_row.details.text()
    assert panel.own_time.text() == "24:10 / 60m"
    assert panel.history_toggle.isChecked()
    assert panel.collapse_panel.toolTip() == "Collapse panel"
    assert panel.format_duration(3600) == "1h 00m"

    english = OUTPUT / "native-en.png"
    if not panel.grab().save(str(english)):
        raise RuntimeError(f"could not save {english}")

    panel.close()

    # A 150% text-size approximation at the documented 280px minimum width.
    # Long translated labels and names must reflow instead of disappearing
    # behind the disabled horizontal scrollbar.
    large_font = QtGui.QFont(family, 9)
    large_font.setPointSizeF(13.5)
    app.setFont(large_font)
    controller.locale = "ko"
    controller.online["group"]["name"] = "아주 긴 이름의 주말 아침 스터디방"
    long_member = next(
        member
        for member in controller.online["members"]
        if member["user_id"] == "friend-a"
    )
    long_member["display_name"] = "아침마다도서관창가에서공부하는친구"
    long_member["active_seconds"] = 6 * 3600 + 25 * 60
    large = StudyPanel(controller)
    large.resize(280, 760)
    large.show()
    app.processEvents()

    large_row = large.member_rows["friend-a"]
    assert large._own_compact
    assert large_row._compact
    assert large.member_scroll.horizontalScrollBar().maximum() == 0
    assert large.member_body.width() <= large.member_scroll.viewport().width()
    assert large_row.identity.toolTip().startswith("아침마다도서관")
    assert large.own_time.geometry().right() <= large.content.width()
    assert large.own_answers.geometry().right() <= large.content.width()

    korean_large = OUTPUT / "native-ko-large-280.png"
    if not large.grab().save(str(korean_large)):
        raise RuntimeError(f"could not save {korean_large}")

    controller.locale = "en"
    controller.online["group"]["name"] = "Saturday Morning Language Study Room"
    long_member["display_name"] = "FriendWithAnIntentionallyLongDisplayName"
    large.resize(320, 760)
    large.refresh()
    app.processEvents()
    large_row = large.member_rows["friend-a"]
    assert large_row._compact
    assert large.member_scroll.horizontalScrollBar().maximum() == 0
    assert large.member_body.width() <= large.member_scroll.viewport().width()
    assert large_row.identity.toolTip().startswith("FriendWith")
    assert "\u200b" in large_row.identity_text.text()

    english_large = OUTPUT / "native-en-large-320.png"
    if not large.grab().save(str(english_large)):
        raise RuntimeError(f"could not save {english_large}")

    print(f"native panel smoke ok: {korean}")
    print(f"native panel smoke ok: {english}")
    print(f"native panel smoke ok: {korean_large}")
    print(f"native panel smoke ok: {english_large}")
    large.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
