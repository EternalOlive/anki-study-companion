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
        "QLineEdit",
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
    qt.QPointF = QtCore.QPointF
    for name in ("QColor", "QFontMetrics", "QPainter", "QPalette", "QPen"):
        setattr(qt, name, getattr(QtGui, name))
    sys.modules["aqt"] = aqt
    sys.modules["aqt.qt"] = qt

    package = types.ModuleType("study_companion")
    package.__path__ = [str(ROOT / "study_companion")]
    sys.modules["study_companion"] = package

    from study_companion.panel import StudyPanel
    from study_companion.tracker import StudyTracker, TIMEZONE

    return QtGui, QtWidgets, StudyPanel, StudyTracker, TIMEZONE


def _room_day(current):
    from study_companion.study_day import study_day

    return study_day(current, "Asia/Seoul")


def _assert_unclipped(panel):
    """Wrapped labels in the new sections get the height their text needs.

    The real dock scrolls the panel, so first give it the height its layout
    asks for at this width; a squeezed fixed-height window is not a clip.
    """
    from PyQt6.QtWidgets import QApplication

    needed = panel.layout().totalHeightForWidth(panel.width())
    if needed > panel.height():
        panel.resize(panel.width(), needed)
    QApplication.processEvents()
    for label in (panel.room_timeline_detail, panel.room_timeline_legend,
                  panel.weekly_legend, panel.weekly_day_detail):
        if label.isVisible():
            assert label.height() >= label.heightForWidth(label.width()), (label.height(), label.heightForWidth(label.width()), label.width(), panel.content.width(), panel.width())
            assert label.geometry().right() <= panel.content.width()
    for widget in (panel.room_timeline, panel.weekly_chart):
        if widget.isVisible():
            assert widget.width() <= panel.content.width()


class FakeController:
    def __init__(self, tracker, current):
        self.tracker = tracker
        self.locale = "ko"
        self.online = {
            "group": {
                "id": "group-1",
                "name": "goyori",
                "time_zone": "Asia/Seoul",
                "day_start_hour": 4,
            },
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
                    "activity_known": True,
                    "activity_buckets": [
                        {"slot": 30, "answer_count": 4, "time_ms": 72000},
                        {"slot": 31, "answer_count": 3, "time_ms": 51000},
                        {"slot": 62, "answer_count": 7, "time_ms": 98000},
                    ],
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
                    "activity_known": False,
                    "activity_buckets": [],
                },
            ],
        }
        self.dialog_pages = []
        self.status_snapshot = {}
        self.sync_in_flight = False
        self.review_query_in_flight = False
        self.review_dirty = False
        self.review_upload_requested = False
        self.pokes_supported = True
        self.poked = []

    def t(self, korean, english):
        return english if self.locale == "en" else korean

    def _current_member_status(self, member):
        return "online" if member.get("status") == "stopped" else member.get("status")

    def display_member_name(self, member):
        return member.get("display_name", "Friend")

    def pokes_available(self):
        return self.pokes_supported

    def poke_member(self, member):
        self.poked.append(member.get("user_id"))
        return True

    def show_dialog(self, *, page=None):
        self.dialog_pages.append(page)

    def sync_async(self, force=False):
        pass

    def update_daily_goals(self, *, time_goal_minutes=None, card_goal=None):
        if time_goal_minutes is not None:
            self.tracker.time_goal_minutes = time_goal_minutes
        if card_goal is not None:
            self.tracker.card_goal = card_goal
        return True

    def record_status_snapshot(self):
        return self.status_snapshot

    def refresh_review_history(self):
        pass

    def save(self):
        pass

    def study_record(self, current):
        return self.tracker.today(current)

    def weekly_record(self, current):
        days = []
        for offset, answers in zip(range(6, -1, -1), (18, 0, 24, 31, 12, 37, 48)):
            days.append({
                "day": (_room_day(current) - timedelta(days=offset)).isoformat(),
                "answers": answers,
                "seconds": answers * 32,
            })
        return {
            "days": days,
            "answers": sum(day["answers"] for day in days),
            "seconds": sum(day["seconds"] for day in days),
            "active_days": sum(day["answers"] > 0 for day in days),
            "previous_answers": 143,
            "previous_seconds": 4620,
            "as_of": current.isoformat(),
        }


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
    room_today = _room_day(current)
    # Past room days cached per room; the day three days ago failed (gap).
    controller.online["room_week_stats"] = {
        "group-1": {
            (room_today - timedelta(days=offset)).isoformat(): {
                "friend-a": answers_a, "friend-b": answers_b,
            }
            for offset, answers_a, answers_b in (
                (6, 30, 5), (5, 22, 0), (4, 41, 12), (2, 15, 20), (1, 52, 9),
            )
        }
    }
    local_record = {"seconds": 1450, "answers": 48}
    controller.study_record = lambda current: dict(local_record)
    controller.online["members"].append({
        "user_id": "self", "study_day": room_today.isoformat(),
        "active_seconds": 30 * 60, "answer_count": 60,
        "activity_known": True,
        "activity_buckets": [
            {"slot": 30, "answer_count": 4, "time_ms": 60000},
            {"slot": 40, "answer_count": 6, "time_ms": 80000},
        ],
    })
    panel = StudyPanel(controller)
    panel.resize(340, 900)
    panel.show()
    app.processEvents()

    first_key = panel.member_order[0]
    first_row = panel.member_rows[first_key]
    friend = first_row.member
    friend["current_deck_name"] = "영어::<단어>"
    friend["deck_updated_at"] = current.isoformat()
    panel.refresh()
    assert first_row.deck.text() == "영어::<단어>"
    assert first_row.deck.isVisible()
    assert not first_row.expanded
    friend["deck_updated_at"] = (current - timedelta(seconds=100)).isoformat()
    panel.refresh()
    assert first_row.deck.isVisible()
    friend["deck_updated_at"] = (current - timedelta(days=1)).isoformat()
    panel.refresh()
    assert not first_row.deck.isVisible()
    friend["deck_updated_at"] = current.isoformat()
    friend["status"] = "paused"
    panel.refresh()
    assert "공부 중 아님" not in first_row.details.text()
    assert first_row.deck.isVisible()
    friend["status"] = "offline"
    panel.refresh()
    assert not first_row.deck.isVisible()
    friend["status"] = "studying"
    panel.refresh()
    # Poke: shown on friends' rows only, disabled during the cooldown, hidden
    # entirely when the server lacks the poke RPCs.
    assert first_row.poke.isVisible() and first_row.poke.isEnabled()
    assert first_row.poke.text() == "찌르기"
    assert "self" not in panel.member_rows
    first_row.poke.click()
    app.processEvents()
    assert controller.poked == [friend["user_id"]]
    assert not first_row.poke.isEnabled()
    first_row.poke.click()
    assert controller.poked == [friend["user_id"]]
    panel.refresh()
    assert not first_row.poke.isEnabled()
    assert panel.member_rows["friend-b"].poke.isEnabled()
    controller.pokes_supported = False
    panel.refresh()
    assert not first_row.poke.isVisible()
    assert not panel.poke_column.isVisible()
    controller.pokes_supported = True
    panel.refresh()
    first_row.identity.click()
    panel.history_toggle.setChecked(True)
    app.processEvents()

    assert panel.width() == 340
    assert panel.room_presence.text() == "3명 접속 중 · 나 포함"
    # Room timeline: slot leader colors, ties gray, legend counts sole leads.
    from study_companion.room_activity import FRIEND_COLORS, TIE_COLOR, slot_leader
    room_timeline = panel.room_timeline
    assert panel.room_activity.isVisible()
    assert panel.room_activity_title.text() == "방 시간대 · 10분 1등"
    assert slot_leader(room_timeline.rankings[30]) is None
    assert slot_leader(room_timeline.rankings[40]) == "self"
    assert slot_leader(room_timeline.rankings[62]) == "friend-a"
    assert room_timeline.colors["friend-a"] == FRIEND_COLORS[0]
    assert room_timeline.colors["friend-b"] == FRIEND_COLORS[1]
    assert room_timeline.colors["self"] == panel.my_color()
    assert TIE_COLOR not in room_timeline.colors.values()
    assert room_timeline.selected_slot == 62
    assert panel.room_timeline_detail.text() == "14:20–14:30 · 1등 K7M-2RX 7회"
    assert room_timeline.describe(30) == "09:00–09:10 · 1등 K7M-2RX 4회 · 1등 나 4회"
    assert "1등&nbsp;2번" in panel.room_timeline_legend.text()
    assert "K7M-2RX 1등 2번" in panel.room_timeline_legend.accessibleName()
    assert "나 1등 1번" in panel.room_timeline_legend.accessibleName()
    assert "T4N-8WA" not in panel.room_timeline_legend.accessibleName()
    assert "14:20–14:30" in room_timeline.accessibleName()
    assert "14:20–14:30" not in room_timeline.toolTip()
    assert first_row.details.isVisible()
    assert first_row.activity_timeline.isVisible()
    assert "09:00–09:10" in first_row.activity_timeline.accessibleName()
    assert "Asia/Seoul · 04:00" in first_row.activity_timeline.accessibleName()
    assert "4회" in first_row.activity_timeline.accessibleName()
    # Hovering an empty area must not show the full per-bin accessible list.
    assert "09:00–09:10" not in first_row.activity_timeline.toolTip()
    from PyQt6.QtCore import Qt, QPoint
    from PyQt6.QtTest import QTest
    timeline = first_row.activity_timeline
    assert timeline.selected_slot == 62
    assert timeline.is_today
    QTest.keyClick(room_timeline, Qt.Key.Key_Left)
    assert room_timeline.selected_slot == 40
    assert panel.room_timeline_detail.text() == "10:40–10:50 · 1등 나 6회"
    QTest.mouseClick(room_timeline, Qt.MouseButton.LeftButton,
                     pos=QPoint(1 + round(30.5 * (room_timeline.width() - 2) / 144), 8))
    assert room_timeline.selected_slot == 30
    assert "1등 나 4회" in panel.room_timeline_detail.text()
    QTest.keyClick(timeline, Qt.Key.Key_Left)
    assert timeline.selected_slot == 31
    timeline.update_activity(first_row.member)
    assert timeline.selected_slot == 31
    QTest.mouseClick(timeline, Qt.MouseButton.LeftButton,
                     pos=QPoint(1 + round(30.5 * (timeline.width() - 2) / 144), 35))
    assert timeline.selected_slot == 30
    assert "09:00–09:10" in timeline.accessibleDescription()
    saved_buckets = dict(timeline.buckets)
    timeline.update_activity(dict(first_row.member, activity_error=True,
                                  activity_known=False, activity_buckets=[]))
    assert timeline.buckets == saved_buckets
    controller.online["group"]["time_zone"] = "America/New_York"
    timeline.update_activity(dict(
        first_row.member,
        study_day=panel.current_room_day().isoformat(),
        activity_error=True,
        activity_known=False,
        activity_buckets=[],
    ))
    assert not timeline.buckets
    controller.online["group"]["time_zone"] = "Asia/Seoul"
    timeline.update_activity(first_row.member)
    current_friend = first_row.member
    first_row.update_member(dict(current_friend, study_day=yesterday))
    assert "시간대 기록 없음" in first_row.activity_timeline.accessibleName()
    assert "09:00–09:10" not in first_row.activity_timeline.accessibleName()
    first_row.update_member(current_friend)
    assert "시간대 기록 없음" in panel.member_rows["friend-b"].activity_timeline.accessibleName()
    assert panel.history_body.isVisible()
    chart = panel.weekly_chart
    assert chart.isVisible()
    assert len(chart.days) == 7 and chart.days[-1] == room_today.isoformat()
    assert [item["key"] for item in chart.series] == ["self", "friend-a", "friend-b"]
    assert chart.series[0]["me"] and chart.series[0]["values"][-1] == 48
    assert chart.series[1]["values"] == [30, 22, 41, None, 15, 52, 42]
    assert chart.series[2]["color"] == FRIEND_COLORS[1]
    assert "오늘 진행 중" in chart.day_description(6)
    assert "K7M-2RX 확인 못 함" in chart.day_description(3)
    assert "끊긴 선" in chart.toolTip()
    assert panel.weekly_legend.isVisible()
    assert "이전 7일보다" in panel.weekly_summary.toolTip()
    assert "\n" not in panel.weekly_summary.text()
    assert not panel.deck_history_body.isVisible()
    panel.deck_history_title.click()
    assert panel.deck_history_body.isVisible()
    assert len(panel.member_rows) == 2
    assert first_row.dot.text() == "●" and first_row.dot.isVisible()
    online_row = panel.member_rows["friend-b"]
    assert online_row.dot.text() == "●" and online_row.dot.isVisible()
    assert "font-size: 8px" in online_row.dot.styleSheet()
    assert "font-size: 8px" in panel.own_dot.styleSheet()
    assert online_row.dot.styleSheet() != first_row.dot.styleSheet()
    assert panel.own_time.accessibleName() == "공부 시간 목표 수정 · 24:10 / 1:00:00"
    assert panel.own_answers.accessibleName() == "답변 목표 수정 · 48 / 100"
    cached_members = controller.online["members"]
    controller.online["members"] = [
        member for member in cached_members if member.get("user_id") != "self"
    ]
    # Live time advances independently of the answer-history snapshot.
    tracker.records[today]["seconds"] = 1480
    local_record.update(answers=49)
    panel.refresh()
    assert panel.own_time.accessibleName() == "공부 시간 목표 수정 · 24:40 / 1:00:00"
    assert panel.own_answers.accessibleName() == "답변 목표 수정 · 49 / 100"
    controller.online["members"] = cached_members
    tracker.records[today]["seconds"] = 1450
    local_record.update(answers=48)
    panel.refresh()
    first_row = panel.member_rows[first_key]
    online_row = panel.member_rows["friend-b"]
    from PyQt6.QtTest import QTest
    from PyQt6.QtCore import Qt
    QTest.mouseClick(panel.own_time, Qt.MouseButton.LeftButton)
    assert panel.goal_editor is not None and panel.goal_editor.kind == "time"
    panel.goal_editor.input.setText("²")
    assert not panel.goal_editor.save.isEnabled()
    panel.goal_editor.input.setText("9999999")
    assert len(panel.goal_editor.input.text()) == 6
    assert not panel.goal_editor.save.isEnabled()
    panel.goal_editor.input.setText("75")
    panel.refresh()
    assert panel.goal_editor.input.text() == "75"
    QTest.keyClick(panel.goal_editor.input, Qt.Key.Key_Return)
    app.processEvents()
    assert tracker.time_goal_minutes == 75
    assert panel.goal_editor is None
    QTest.mouseClick(panel.own_answers, Qt.MouseButton.LeftButton)
    assert panel.goal_editor is not None and panel.goal_editor.kind == "answers"
    QTest.keyClick(panel.goal_editor, Qt.Key.Key_Escape)
    app.processEvents()
    assert panel.goal_editor is None
    tracker.time_goal_minutes = 60
    panel.refresh()
    mine = next(m for m in controller.online["members"] if m["user_id"] == "self")
    mine.update(activity_known=True, activity_buckets=[
        {"slot": 30, "answer_count": 4, "time_ms": 72000},
    ])
    panel.refresh()
    assert not panel.own_activity_timeline.isVisible()
    panel.own_activity_toggle.click()
    assert panel.own_activity_timeline.isVisible()
    assert "09:00–09:10" in panel.own_activity_timeline.accessibleName()
    mine["activity_error"] = True
    panel.refresh()
    assert "동기화 지연" in panel.own_activity_timeline.accessibleName()
    mine.pop("activity_error")
    mine["activity_buckets"] = []
    panel.refresh()
    assert "오늘 답변 기록 없음" in panel.own_activity_timeline.accessibleName()
    mine["study_day"] = yesterday
    panel.refresh()
    assert "시간대 기록 없음" in panel.own_activity_timeline.accessibleName()
    mine["study_day"] = today
    panel.own_activity_toggle.click()
    panel.refresh()
    assert "PC 공부 시간" in panel.own_title.toolTip()
    assert "모바일" in panel.own_title.toolTip()
    assert panel.time_caption.text() == "공부 시간"
    assert panel.answer_caption.text() == "답변"
    assert first_row.answers.text().isdigit()
    assert panel.collapse_panel.toolTip() == "패널 접기"
    assert panel.format_duration(3600) == "1:00:00"
    assert panel.format_clock(3661) == "1:01:01"
    assert panel.format_clock(3599) == "59:59"
    assert panel.format_clock(0) == "00:00"
    assert panel.format_clock(36000) == "10:00:00"
    controller.status_snapshot = {
        "primary_issue": "upload",
        "errors": {"upload": "timeout"},
    }
    panel.refresh()
    assert panel.error_text.text() == "공유 지연"
    assert panel.retry.isVisible() and panel.retry.isEnabled()
    panel.error_text.click()
    assert controller.dialog_pages[-1] == "record_status"
    controller.sync_in_flight = True
    panel.refresh()
    assert not panel.retry.isEnabled()
    controller.sync_in_flight = False
    controller.status_snapshot = {"primary_issue": "pending", "errors": {}}
    panel.refresh()
    assert panel.error_text.text() == "공유 대기"
    assert not panel.retry.isVisible()
    controller.status_snapshot = {}
    panel.refresh()
    # A friend without deck/goals can still open the activity timeline; stale
    # time is shown once in the summary.
    empty_friend = dict(online_row.member, status="offline", time_goal_minutes=0,
                        card_goal=0, current_deck_name=None)
    online_row.update_member(empty_friend)
    online_row.identity.click()
    assert not online_row.expanded and not online_row.details.text()
    assert not online_row.identity.isCheckable()
    assert "+" not in online_row.identity_text.text()
    assert "시간대 기록 없음" in online_row.activity_timeline.accessibleName()
    assert "갱신" not in online_row.identity_text.text()
    assert " · " not in online_row.identity_text.text()
    assert not online_row.dot.isVisible()
    assert "목표 없음" not in online_row.details.text()
    online_row.update_member(dict(empty_friend, activity_known=True, activity_buckets=[]))
    assert "오늘 답변 기록 없음" in online_row.activity_timeline.accessibleName()
    assert online_row.activity_timeline.height() < 50
    online_row.identity.click()
    assert online_row.expanded
    online_row.update_member(dict(empty_friend, activity_error="timeout"))
    assert "시간대 동기화 지연" in online_row.activity_timeline.accessibleName()
    assert online_row.activity_retry.isVisible()
    assert not online_row.activity_timeline.isVisible()
    online_row.update_member(dict(empty_friend, active_seconds=None, answer_count=None))
    assert online_row.time.text() == "—" and online_row.answers.text() == "—"
    online_row.update_member(controller.online["members"][1])
    # Clicking the numeric side of the row and keyboard Space both toggle details.
    chart.setFocus()
    QTest.keyClick(chart, Qt.Key.Key_Space)
    assert panel.weekly_day_detail.isVisible()
    assert chart.selected_index == 6
    assert "나 48회" in panel.weekly_day_detail.text()
    assert "K7M-2RX 42회" in panel.weekly_day_detail.text()
    QTest.keyClick(chart, Qt.Key.Key_Left)
    assert chart.selected_index == 5
    assert "K7M-2RX 52회" in panel.weekly_day_detail.text()
    panel.refresh()
    assert chart.selected_index == 5
    QTest.keyClick(chart, Qt.Key.Key_Right)
    assert "오늘 진행 중" in chart.accessibleDescription()
    _assert_unclipped(panel)
    assert "마지막 확인" in panel.weekly_summary.toolTip()
    numeric_point = first_row.time.geometry().center()
    QTest.mouseClick(first_row.identity, Qt.MouseButton.LeftButton, pos=numeric_point)
    assert not first_row.expanded
    first_row.identity.setFocus()
    QTest.keyClick(first_row.identity, Qt.Key.Key_Space)
    assert first_row.expanded
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

    mine["activity_buckets"] = [
        {"slot": 30, "answer_count": 4, "time_ms": 60000},
        {"slot": 40, "answer_count": 6, "time_ms": 80000},
    ]
    panel.refresh()
    app.processEvents()
    _assert_unclipped(panel)
    OUTPUT.mkdir(exist_ok=True)
    korean = OUTPUT / "native-ko.png"
    if not panel.grab().save(str(korean)):
        raise RuntimeError(f"could not save {korean}")
    room_crop = OUTPUT / "native-room-timeline.png"
    assert panel.room_activity.grab().save(str(room_crop))
    weekly_crop = OUTPUT / "native-weekly-lines.png"
    assert panel.history_body.grab().save(str(weekly_crop))

    original_order = list(panel.member_order)
    controller.online["members"].reverse()
    controller.locale = "en"
    panel.refresh()
    app.processEvents()

    assert panel.member_order == original_order
    assert first_row.expanded and first_row.details.isVisible()
    assert panel.own_title.text() == "You · today"
    assert "PC study time updates live" in panel.own_title.toolTip()
    assert first_row.deck.text() == "영어::<단어>"
    assert panel.own_time.accessibleName() == "Edit study time goal · 24:10 / 1:00:00"
    assert panel.history_toggle.isChecked()
    assert panel.weekly_title.text() == "Recent 7 days"
    assert "today in progress" in panel.weekly_chart.day_description(6)
    assert panel.room_presence.text() == "3 online · including you"
    assert panel.room_activity_title.text() == "Room timeline · top per 10 min"
    assert "#1 K7M-2RX 7" in panel.room_timeline.describe(62)
    assert "1st&nbsp;×2" in panel.room_timeline_legend.text()
    _assert_unclipped(panel)
    assert panel.collapse_panel.toolTip() == "Collapse panel"
    assert panel.format_duration(3600) == "1:00:00"

    english = OUTPUT / "native-en.png"
    if not panel.grab().save(str(english)):
        raise RuntimeError(f"could not save {english}")

    original_palette = app.palette()
    dark = QtGui.QPalette(original_palette)
    for role in (QtGui.QPalette.ColorRole.Window, QtGui.QPalette.ColorRole.Base,
                 QtGui.QPalette.ColorRole.Button):
        dark.setColor(role, QtGui.QColor("#292929"))
    for role in (QtGui.QPalette.ColorRole.WindowText, QtGui.QPalette.ColorRole.Text,
                 QtGui.QPalette.ColorRole.ButtonText):
        dark.setColor(role, QtGui.QColor("#eeeeee"))
    dark.setColor(QtGui.QPalette.ColorRole.Midlight, QtGui.QColor("#3c3c3c"))
    app.setPalette(dark)
    app.processEvents()
    controller.locale = "ko"
    tracker.time_goal_minutes = 300
    panel.refresh()
    app.processEvents()
    assert "5:00:00" in panel.own_time.accessibleName()
    expected_foreground = panel.palette().color(QtGui.QPalette.ColorRole.WindowText).name()
    assert expected_foreground in first_row.identity_text.styleSheet()
    _assert_unclipped(panel)
    dark_shot = OUTPUT / "native-ko-dark.png"
    assert panel.grab().save(str(dark_shot))
    assert panel.room_activity.grab().save(str(OUTPUT / "native-room-timeline-dark.png"))
    assert panel.history_body.grab().save(str(OUTPUT / "native-weekly-lines-dark.png"))
    tracker.time_goal_minutes = 60
    app.setPalette(original_palette)
    app.processEvents()

    # native-ko.png is already at the 280px minimum (collapse/expand resets
    # the width); also check the common 320px dock width with the normal font.
    panel.resize(320, panel.height())
    panel.refresh()
    app.processEvents()
    _assert_unclipped(panel)
    narrow = OUTPUT / "native-ko-320.png"
    assert panel.grab().save(str(narrow))

    saved_group = controller.online["group"]
    saved_members = controller.online["members"]
    controller.online["group"] = None
    controller.online["members"] = None
    no_room = StudyPanel(controller)
    no_room.resize(280, 500)
    no_room.show()
    app.processEvents()
    assert no_room.no_room_actions.isVisible()
    assert not no_room.room_activity.isVisible()
    assert not no_room.member_empty.isVisible()
    no_room.create_room.click()
    no_room.join_room.click()
    assert controller.dialog_pages[-2:] == ["create", "join"]
    no_room.close()
    controller.online["group"] = saved_group
    controller.online["members"] = saved_members

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
    large.resize(280, 900)
    large.show()
    app.processEvents()

    large_row = large.member_rows["friend-a"]
    large_unknown_row = large.member_rows["friend-b"]
    large_unknown_row.identity.click()
    large.history_toggle.setChecked(True)
    app.processEvents()
    assert large._own_compact
    assert large_row._compact
    assert large.member_body.width() <= large.content.width()
    assert large_row.compact_metrics.isVisible()
    assert "회" in large_row.compact_metrics.text()
    assert not large_row.time.isVisible()
    assert large_row.identity.toolTip().startswith("아침마다도서관")
    assert large.own_time.geometry().right() <= large.content.width()
    assert large.own_answers.geometry().right() <= large.content.width()
    QTest.keyClick(large.own_time, Qt.Key.Key_Return)
    app.processEvents()
    assert large.goal_editor is not None
    available = large.own_time.screen().availableGeometry()
    assert available.contains(large.goal_editor.geometry().topLeft())
    assert available.contains(large.goal_editor.geometry().bottomRight())
    QTest.keyClick(large.goal_editor, Qt.Key.Key_Escape)
    assert large_unknown_row.activity_timeline.isVisible()
    assert "시간대 기록 없음" in large_unknown_row.activity_timeline.accessibleName()
    assert large.weekly_chart.width() <= large.content.width()
    _assert_unclipped(large)

    korean_large = OUTPUT / "native-ko-large-280.png"
    if not large.grab().save(str(korean_large)):
        raise RuntimeError(f"could not save {korean_large}")

    # The real dock wraps StudyPanel in an outer scroll area. With expanded
    # history and 150% text, every section remains reachable vertically.
    outer_scroll = QtWidgets.QScrollArea()
    outer_scroll.setWidgetResizable(True)
    outer_scroll.setWidget(large)
    outer_scroll.resize(300, 650)
    outer_scroll.show()
    large.deck_history_title.setChecked(True)
    app.processEvents()
    assert outer_scroll.verticalScrollBar().maximum() > 0
    outer_scroll.ensureWidgetVisible(large.history_summary)
    app.processEvents()
    assert outer_scroll.verticalScrollBar().value() > 0
    assert outer_scroll.grab().save(str(OUTPUT / "native-ko-large-scroll-bottom.png"))
    outer_scroll.takeWidget()
    outer_scroll.close()
    large.setParent(None)
    large.show()

    controller.locale = "en"
    controller.online["group"]["name"] = "Saturday Morning Language Study Room"
    long_member["display_name"] = "FriendWithAnIntentionallyLongDisplayName"
    large.resize(320, 760)
    large.refresh()
    app.processEvents()
    large_row = large.member_rows["friend-a"]
    assert large_row._compact
    assert large.member_body.width() <= large.content.width()
    assert "answers" in large_row.compact_metrics.text()
    assert large_row.identity.toolTip().startswith("FriendWith")
    assert "\u200b" in large_row.identity_text.text()

    _assert_unclipped(large)
    english_large = OUTPUT / "native-en-large-320.png"
    if not large.grab().save(str(english_large)):
        raise RuntimeError(f"could not save {english_large}")

    print(f"native panel smoke ok: {korean}")
    print(f"native panel smoke ok: {english}")
    print(f"native panel smoke ok: {korean_large}")
    print(f"native panel smoke ok: {english_large}")
    print(f"native panel smoke ok: {narrow}")
    print(f"native panel smoke ok: {dark_shot}")
    print(f"native panel smoke ok: {room_crop}")
    print(f"native panel smoke ok: {weekly_crop}")
    large.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
