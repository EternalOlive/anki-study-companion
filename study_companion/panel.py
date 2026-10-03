"""Native Qt study-room panel used by the Anki add-on.

The panel deliberately owns presentation only.  Tracking, member freshness,
localisation, and network actions stay on the controller.
"""

from __future__ import annotations

import html
import re
from datetime import datetime

from aqt.qt import (
    QButtonGroup,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QPainter,
    QPalette,
    QPen,
    QToolButton,
    Qt,
    QVBoxLayout,
    QWidget,
)

from .history import get_comparison
from .tracker import TIMEZONE, answers_per_minute


def _now() -> datetime:
    return datetime.now(TIMEZONE)


def _set_font(widget: QWidget, *, scale: float = 1.0, bold: bool = False) -> None:
    font = widget.font()
    size = font.pointSizeF()
    if size > 0:
        font.setPointSizeF(size * scale)
    font.setBold(bold)
    widget.setFont(font)


def _allow_anywhere_wrap(value: str) -> str:
    """Give Qt wrap points inside unusually long unbroken display names."""

    return re.sub(r"\S{12,}", lambda match: "\u200b".join(match.group(0)), value)


class PanelToggleButton(QToolButton):
    """Theme-aware dock control with a persistent, keyboard-visible outline."""

    def __init__(self, parent=None, *, expand=False):
        super().__init__(parent)
        self.expand = expand
        self.setFixedSize(32, 32)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        foreground = self.palette().color(QPalette.ColorRole.WindowText)
        background = self.palette().color(QPalette.ColorRole.Window)
        painter.fillRect(self.rect(), background)
        fill = self.palette().color(QPalette.ColorRole.WindowText)
        fill.setAlpha(32 if self.isDown() else 20 if self.underMouse() else 8)
        border = self.palette().color(QPalette.ColorRole.WindowText)
        border.setAlpha(220 if self.hasFocus() else 100 if self.underMouse() else 65)
        painter.setBrush(fill)
        painter.setPen(QPen(border, 2 if self.hasFocus() else 1))
        painter.drawRoundedRect(2, 2, 28, 28, 5, 5)
        painter.setPen(QPen(foreground, 2, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        start, end = (18, 13) if self.expand else (13, 18)
        painter.drawLine(start, 11, end, 16)
        painter.drawLine(end, 16, start, 21)


class ActivityTimeline(QWidget):
    """A compact, accessible view of recorded answers across one day."""

    def __init__(self, panel: "StudyPanel", parent=None):
        super().__init__(parent)
        self.panel = panel
        self.known = False
        self.error = False
        self.buckets: dict[int, dict] = {}
        self.setMinimumHeight(45)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

    def update_activity(self, member: dict) -> None:
        study_day = member.get("study_day")
        matches_today = study_day is None or str(study_day) == _now().date().isoformat()
        self.known = member.get("activity_known") is True and matches_today
        self.error = bool(member.get("activity_error")) and matches_today
        self.setMinimumHeight(max(45, self.fontMetrics().height() * 3))
        self.buckets = {}
        if self.known:
            for bucket in member.get("activity_buckets") or []:
                try:
                    slot = int(bucket.get("slot"))
                    answers = max(0, int(bucket.get("answer_count") or 0))
                    time_ms = max(0, int(bucket.get("time_ms") or 0))
                except (AttributeError, TypeError, ValueError):
                    continue
                if 0 <= slot < 96 and (answers or time_ms):
                    self.buckets[slot] = {
                        "answer_count": answers,
                        "time_ms": time_ms,
                    }
        descriptions = [self._description(slot) for slot in sorted(self.buckets)]
        if self.error:
            summary = self.panel.tr("시간대 동기화 지연", "Activity sync delayed")
        elif not self.known:
            summary = self.panel.tr("시간대 기록 없음", "Timeline unavailable")
        elif not descriptions:
            summary = self.panel.tr("오늘 답변 기록 없음", "No answers recorded today")
        else:
            summary = self.panel.tr("오늘 활동: ", "Today's activity: ") + "; ".join(descriptions)
        summary += self.panel.tr(" · 한국 시간(UTC+9)", " · UTC+9")
        self.setAccessibleName(summary)
        self.setToolTip(summary)
        self.update()

    def _description(self, slot: int) -> str:
        bucket = self.buckets[slot]
        start_minutes = slot * 15
        end_minutes = start_minutes + 15
        start = f"{start_minutes // 60:02d}:{start_minutes % 60:02d}"
        end = "24:00" if end_minutes == 1440 else f"{end_minutes // 60:02d}:{end_minutes % 60:02d}"
        answers = bucket["answer_count"]
        duration = self.panel.format_clock(round(bucket["time_ms"] / 1000))
        return self.panel.tr(
            f"{start}–{end} (UTC+9) · {answers}회 · 기록 시간 {duration}",
            f"{start}–{end} (UTC+9) · {answers} answers · recorded time {duration}",
        )

    def _slot_at(self, x: int) -> int | None:
        left, right = 1, max(2, self.width() - 1)
        if x < left or x >= right:
            return None
        return min(95, max(0, int((x - left) * 96 / max(1, right - left))))

    def mouseMoveEvent(self, event) -> None:
        slot = self._slot_at(int(event.position().x()))
        if slot in self.buckets:
            self.setToolTip(self._description(slot))
        else:
            self.setToolTip(self.accessibleName())
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self.setToolTip(self.accessibleName())
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        foreground = self.palette().color(QPalette.ColorRole.WindowText)
        muted = self.palette().color(QPalette.ColorRole.WindowText)
        muted.setAlpha(170)
        font = painter.font()
        font.setPointSizeF(max(7.0, font.pointSizeF() * 0.72))
        painter.setFont(font)
        metrics = painter.fontMetrics()
        label_y = max(metrics.ascent() + 2, self.height() - 4)
        baseline_y = max(15, label_y - metrics.height() - 4)
        bar_top = max(3, baseline_y - 12)
        if self.known and not self.error and self.buckets:
            painter.setPen(QPen(muted, 1))
            painter.drawLine(1, baseline_y, max(1, self.width() - 2), baseline_y)
            width = max(1, self.width() - 2)
            for slot in self.buckets:
                x1 = 1 + round(slot * width / 96)
                x2 = 1 + round((slot + 1) * width / 96)
                painter.fillRect(x1, bar_top, max(1, x2 - x1), baseline_y - bar_top, foreground)

        if self.error or not self.known or not self.buckets:
            empty = self.panel.tr(
                "시간대 동기화 지연" if self.error else "오늘 답변 기록 없음" if self.known else "시간대 기록 없음",
                "Activity sync delayed" if self.error else "No answers recorded today" if self.known else "Timeline unavailable",
            )
            painter.setPen(foreground)
            painter.drawText(
                self.rect().adjusted(1, 1, -1, -(self.height() - baseline_y + 1)),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                empty,
            )
        painter.setPen(muted)
        labels = ((0, "00"), (24, "06"), (48, "12"), (72, "18"), (96, "24"))
        width = max(1, self.width() - 2)
        for slot, label in labels:
            x = 1 + round(slot * width / 96)
            if slot == 96:
                x -= metrics.horizontalAdvance(label)
            elif slot:
                x -= metrics.horizontalAdvance(label) // 2
            painter.drawText(x, label_y, label)

        if self.hasFocus():
            focus = self.palette().color(QPalette.ColorRole.Highlight)
            painter.setPen(QPen(focus, 1))
            painter.drawRect(0, 0, max(0, self.width() - 1), max(0, self.height() - 1))


class WeeklyDayButton(QToolButton):
    """One keyboard-focusable answer bar in the recent-seven-day view."""

    def __init__(self, panel: "StudyPanel", parent=None):
        super().__init__(parent)
        self.panel = panel
        self.day = ""
        self.answers = 0
        self.seconds = 0
        self.maximum = 1
        self.today = False
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(20)
        self.setFixedHeight(72)
        self.setAutoRaise(True)
        self.clicked.connect(lambda: self.panel.show_weekly_day(self))

    def set_day(self, day: dict, maximum: int, today: str) -> None:
        self.day = str(day.get("day") or "")
        self.answers = max(0, int(day.get("answers") or 0))
        self.seconds = max(0, int(day.get("seconds") or 0))
        self.maximum = max(1, int(maximum))
        self.today = self.day == today
        self.setFixedHeight(max(72, self.fontMetrics().height() * 4))
        try:
            date_label = datetime.fromisoformat(self.day).strftime("%m/%d")
        except ValueError:
            date_label = self.day or "—"
        description = self.panel.tr(
            f"{date_label} · 답변 {self.answers}회 · 공부 시간 {self.panel.format_clock(self.seconds)}",
            f"{date_label} · {self.answers} answers · study time {self.panel.format_clock(self.seconds)}",
        )
        if self.today:
            description += self.panel.tr(" · 오늘 진행 중", " · today in progress")
        self.setAccessibleName(description)
        self.setToolTip(description)
        self.update()

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.panel.show_weekly_day(self)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        foreground = self.palette().color(QPalette.ColorRole.WindowText)
        muted = self.palette().color(QPalette.ColorRole.WindowText)
        muted.setAlpha(170)
        font = painter.font()
        font.setPointSizeF(max(6.5, font.pointSizeF() * 0.68))
        painter.setFont(font)
        metrics = painter.fontMetrics()
        label_top = max(16, self.height() - metrics.height() - 3)
        bar_bottom = max(12, label_top - 4)
        available_bar_height = max(6, bar_bottom - 7)
        bar_height = round(available_bar_height * self.answers / self.maximum) if self.answers else 1
        bar_width = max(4, min(14, self.width() - 8))
        bar_x = (self.width() - bar_width) // 2
        color = foreground if self.answers else muted
        painter.fillRect(bar_x, bar_bottom - bar_height, bar_width, bar_height, color)

        painter.setPen(muted)
        try:
            label = datetime.fromisoformat(self.day).strftime("%m/%d")
        except ValueError:
            label = "—"
        painter.drawText(
            self.rect().adjusted(0, label_top, 0, 0),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            label,
        )

        if self.today or self.hasFocus():
            border = self.palette().color(
                QPalette.ColorRole.Highlight if self.hasFocus() else QPalette.ColorRole.Mid
            )
            painter.setPen(QPen(border, 2 if self.hasFocus() else 1))
            painter.drawRoundedRect(1, 1, max(0, self.width() - 3), max(0, self.height() - 3), 3, 3)


class MemberRow(QWidget):
    """A stable, expandable friend row.

    Rows are updated in place so a refresh never changes keyboard focus or
    collapses a detail the learner intentionally opened.
    """

    def __init__(self, panel: "StudyPanel", member: dict, parent=None):
        super().__init__(parent)
        self.panel = panel
        self.member = member
        self.expanded = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 7, 0, 7)
        layout.setSpacing(5)

        self.identity = QPushButton(self)
        self.identity.setObjectName("member_summary")
        self.identity.setFlat(True)
        self.identity.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.identity.setStyleSheet(
            "QPushButton#member_summary { background: transparent; border: 1px solid transparent; border-radius: 4px; }"
            "QPushButton#member_summary[expandable=\"true\"]:hover { background: palette(midlight); }"
            "QPushButton#member_summary:focus { border-color: palette(highlight); }"
        )
        self.identity.clicked.connect(self.toggle_expanded)
        self.summary = QGridLayout(self.identity)
        self.summary.setContentsMargins(3, 3, 3, 3)
        self.summary.setHorizontalSpacing(8)
        self.summary.setVerticalSpacing(4)

        self.dot = QLabel(self.identity)
        self.dot.setText("●")
        self.dot.setFixedWidth(10)
        self.dot.setAlignment(
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter
        )
        self.dot.setAccessibleName(self.panel.tr("공부 중", "Studying"))
        self.summary.addWidget(self.dot, 0, 0)

        self.identity_text = QLabel(self.identity)
        self.identity_text.setTextFormat(Qt.TextFormat.PlainText)
        self.identity_text.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self.identity_text.setWordWrap(True)
        self.identity_text.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.summary.addWidget(self.identity_text, 0, 1)

        self.time = QLabel(self.identity)
        self.time.setMinimumWidth(50)
        self.time.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.summary.addWidget(self.time, 0, 2)

        self.answers = QLabel(self.identity)
        self.answers.setMinimumWidth(44)
        self.answers.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.summary.addWidget(self.answers, 0, 3)
        self.summary.setColumnStretch(1, 1)
        for label in (self.dot, self.time, self.answers):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.identity)

        self.detail_body = QWidget(self)
        detail_layout = QVBoxLayout(self.detail_body)
        detail_layout.setContentsMargins(18, 0, 0, 2)
        detail_layout.setSpacing(4)
        self.details = QLabel(self.detail_body)
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setWordWrap(True)
        detail_layout.addWidget(self.details)
        self.activity_timeline = ActivityTimeline(panel, self.detail_body)
        detail_layout.addWidget(self.activity_timeline)
        self.detail_body.hide()
        layout.addWidget(self.detail_body)

        self._compact = False
        self.update_member(member)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        metrics_width = (
            self.time.sizeHint().width()
            + self.answers.sizeHint().width()
            + self.summary.horizontalSpacing()
        )
        identity_floor = max(110, self.fontMetrics().horizontalAdvance("MMMMMMMMMM"))
        compact = self.width() < metrics_width + identity_floor + 34
        self._set_compact(compact)

    def _set_compact(self, compact: bool) -> None:
        if compact == self._compact:
            return
        self._compact = compact
        self.summary.removeWidget(self.dot)
        self.summary.removeWidget(self.identity_text)
        self.summary.removeWidget(self.time)
        self.summary.removeWidget(self.answers)
        if compact:
            self.summary.addWidget(self.dot, 0, 0)
            self.summary.addWidget(self.identity_text, 0, 1, 1, 3)
            self.summary.addWidget(self.time, 1, 2)
            self.summary.addWidget(self.answers, 1, 3)
        else:
            self.summary.addWidget(self.dot, 0, 0)
            self.summary.addWidget(self.identity_text, 0, 1)
            self.summary.addWidget(self.time, 0, 2)
            self.summary.addWidget(self.answers, 0, 3)

    def toggle_expanded(self) -> None:
        self.expanded = not self.expanded
        self.identity.setChecked(self.expanded)
        self.detail_body.setVisible(self.expanded)

    def update_member(self, member: dict) -> None:
        self.member = member
        foreground = self.panel.palette().color(QPalette.ColorRole.WindowText).name()
        for label in (self.identity_text, self.time, self.answers):
            label.setStyleSheet(f"color: {foreground}; background: transparent;")
        status = self.panel.controller._current_member_status(member)
        name = self.panel.member_name(member)
        status_text = self.panel.status_text(status)
        if status == "offline":
            updated = self.panel.updated_time(member.get("updated_at"))
            status_text = self.panel.tr(f"갱신 {updated}", f"Updated {updated}")
        self.identity_text.setText(
            f"{_allow_anywhere_wrap(name)}  ·  {status_text}"
        )
        self.identity.setToolTip(f"{name} · {status_text}")
        self.panel.update_status_dot(self.dot, status)

        seconds = max(0, int(member.get("active_seconds") or 0))
        answers = max(0, int(member.get("answer_count") or 0))
        self.time.setText(self.panel.format_duration(seconds) if member.get("active_seconds") is not None else "—")
        self.answers.setText(str(answers) if member.get("answer_count") is not None else "—")
        self.time.setAccessibleName(
            self.panel.tr(f"공부 시간 {self.time.text()}", f"Study time {self.time.text()}")
        )
        self.answers.setAccessibleName(
            self.panel.tr(f"답변 {self.answers.text()}", f"Answers {self.answers.text()}")
        )

        time_goal = max(0, int(member.get("time_goal_minutes") or 0))
        answer_goal = max(0, int(member.get("card_goal") or 0))
        deck_name = None
        if status == "studying" and member.get("current_deck_name"):
            try:
                stamp = datetime.fromisoformat(member["deck_updated_at"].replace("Z", "+00:00"))
                if 0 <= (_now() - stamp).total_seconds() <= 90:
                    deck_name = _allow_anywhere_wrap(str(member["current_deck_name"]))
            except (KeyError, TypeError, ValueError):
                pass
        lines = [deck_name] if deck_name else []
        goals = []
        if time_goal:
            goals.append(self.panel.format_clock(time_goal * 60))
        if answer_goal:
            goals.append(self.panel.tr(f"{answer_goal}회", f"{answer_goal} answers"))
        if goals:
            lines.append(self.panel.tr("목표 ", "Goal ") + " · ".join(goals))
        self.details.setText("\n".join(lines))
        self.details.setVisible(bool(lines))
        self.activity_timeline.update_activity(member)
        expandable = True
        changed = self.identity.property("expandable") != expandable
        self.identity.setProperty("expandable", expandable)
        self.identity.setCheckable(expandable)
        self.identity.setFocusPolicy(Qt.FocusPolicy.StrongFocus if expandable else Qt.FocusPolicy.NoFocus)
        self.identity.setCursor(Qt.CursorShape.PointingHandCursor if expandable else Qt.CursorShape.ArrowCursor)
        self.identity.setAccessibleName(f"{name}, {status_text}" + (
            self.panel.tr(". 오늘 활동 보기", ". Show today's activity") if expandable else ""
        ))
        if not expandable:
            self.expanded = False
        self.identity.setChecked(self.expanded)
        self.detail_body.setVisible(self.expanded)
        if changed:
            self.identity.style().unpolish(self.identity)
            self.identity.style().polish(self.identity)
        self._set_compact(
            self.width()
            < self.time.sizeHint().width()
            + self.answers.sizeHint().width()
            + max(110, self.fontMetrics().horizontalAdvance("MMMMMMMMMM"))
            + 34
        )


class StudyPanel(QWidget):
    """Compact native panel for the shared study-room experience."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.member_rows: dict[str, MemberRow] = {}
        self.member_order: list[str] = []
        self.history_mode = "yesterday"
        self.weekly_selected_day: str | None = None
        self.collapsed = False

        self.setObjectName("study_companion_body")
        self.setMinimumWidth(280)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.content = QWidget(self)
        root.addWidget(self.content, 1)
        outer = QVBoxLayout(self.content)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(14)

        self.expand_panel = PanelToggleButton(self, expand=True)
        self.expand_panel.clicked.connect(lambda: self._request_collapsed(False))
        self.expand_panel.hide()
        root.addWidget(self.expand_panel, 0, Qt.AlignmentFlag.AlignHCenter)

        header = QHBoxLayout()
        header.setSpacing(8)
        header_text = QVBoxLayout()
        header_text.setSpacing(1)
        self.room_name = QLabel(self)
        self.room_name.setTextFormat(Qt.TextFormat.PlainText)
        self.room_name.setWordWrap(True)
        self.room_name.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        _set_font(self.room_name, scale=1.1, bold=True)
        self.room_presence = QLabel(self)
        self.room_presence.setWordWrap(True)
        header_text.addWidget(self.room_name)
        header_text.addWidget(self.room_presence)
        header.addLayout(header_text, 1)
        self.manage = QPushButton(self)
        self.manage.setFlat(True)
        self.manage.clicked.connect(self.controller.show_dialog)
        header.addWidget(self.manage)
        self.collapse_panel = PanelToggleButton(self)
        self.collapse_panel.clicked.connect(lambda: self._request_collapsed(True))
        header.addWidget(self.collapse_panel)
        outer.addLayout(header)

        outer.addWidget(self._separator())

        own_header = QHBoxLayout()
        self.own_title = QLabel(self)
        _set_font(self.own_title, bold=True)
        own_header.addWidget(self.own_title)
        own_header.addStretch()
        self.own_dot = QLabel("●", self)
        self.own_dot.setFixedWidth(10)
        self.own_status = QLabel(self)
        own_header.addWidget(self.own_dot)
        own_header.addWidget(self.own_status)
        outer.addLayout(own_header)

        self.own_values = QGridLayout()
        self.own_values.setContentsMargins(0, 0, 0, 0)
        self.own_values.setHorizontalSpacing(12)
        self.own_values.setVerticalSpacing(2)
        self.own_time = QLabel(self)
        _set_font(self.own_time, scale=1.75, bold=True)
        self.own_answers = QLabel(self)
        _set_font(self.own_answers, scale=1.35, bold=True)
        self.time_caption = QLabel(self)
        self.answer_caption = QLabel(self)
        self.own_values.addWidget(self.own_time, 0, 0)
        self.own_values.addWidget(self.own_answers, 0, 1)
        self.own_values.addWidget(self.time_caption, 1, 0)
        self.own_values.addWidget(self.answer_caption, 1, 1)
        self.own_values.setColumnStretch(0, 1)
        self.own_values.setColumnStretch(1, 1)
        outer.addLayout(self.own_values)
        self._own_compact = False

        self.own_activity_toggle = QPushButton(self)
        self.own_activity_toggle.setFlat(True)
        self.own_activity_toggle.setCheckable(True)
        self.own_activity_toggle.setStyleSheet(
            "QPushButton { text-align: left; padding: 4px 0; }"
        )
        outer.addWidget(self.own_activity_toggle)
        self.own_activity_timeline = ActivityTimeline(self, self)
        self.own_activity_timeline.hide()
        self.own_activity_toggle.toggled.connect(self._toggle_own_activity)
        outer.addWidget(self.own_activity_timeline)

        outer.addWidget(self._separator())

        columns = QHBoxLayout()
        self.people_caption = QLabel(self)
        _set_font(self.people_caption, bold=True)
        self.time_column = QLabel(self)
        self.answer_column = QLabel(self)
        self.time_column.setMinimumWidth(50)
        self.answer_column.setMinimumWidth(44)
        self.time_column.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.answer_column.setAlignment(Qt.AlignmentFlag.AlignRight)
        columns.addWidget(self.people_caption, 1)
        columns.addWidget(self.time_column)
        columns.addWidget(self.answer_column)
        outer.addLayout(columns)

        self.member_empty = QLabel(self)
        self.member_empty.setWordWrap(True)
        outer.addWidget(self.member_empty)

        self.member_scroll = QScrollArea(self)
        self.member_scroll.setWidgetResizable(True)
        self.member_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.member_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.member_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.member_body = QWidget(self.member_scroll)
        self.member_layout = QVBoxLayout(self.member_body)
        self.member_layout.setContentsMargins(0, 0, 0, 0)
        self.member_layout.setSpacing(0)
        self.member_layout.addStretch()
        self.member_scroll.setWidget(self.member_body)
        outer.addWidget(self.member_scroll, 1)

        outer.addWidget(self._separator())

        self.history_toggle = QPushButton(self)
        self.history_toggle.setFlat(True)
        self.history_toggle.setCheckable(True)
        self.history_toggle.setStyleSheet(
            "QPushButton { text-align: left; padding: 4px 0; font-weight: bold; }"
        )
        self.history_toggle.toggled.connect(self._toggle_history)
        outer.addWidget(self.history_toggle)

        self.history_body = QWidget(self)
        history_layout = QVBoxLayout(self.history_body)
        history_layout.setContentsMargins(0, 0, 0, 0)
        history_layout.setSpacing(7)
        self.weekly_title = QLabel(self.history_body)
        _set_font(self.weekly_title, bold=True)
        self.weekly_title.setToolTip(self.tr(
            "이 PC에 동기화된 Anki 복습 기록 · 한국 시간(UTC+9)",
            "Anki review history synced to this PC · UTC+9",
        ))
        history_layout.addWidget(self.weekly_title)
        self.weekly_bars = QWidget(self.history_body)
        weekly_bars_layout = QHBoxLayout(self.weekly_bars)
        weekly_bars_layout.setContentsMargins(0, 0, 0, 0)
        weekly_bars_layout.setSpacing(3)
        self.weekly_days = [WeeklyDayButton(self, self.weekly_bars) for _ in range(7)]
        for button in self.weekly_days:
            weekly_bars_layout.addWidget(button, 1)
        history_layout.addWidget(self.weekly_bars)
        self.weekly_day_detail = QLabel(self.history_body)
        self.weekly_day_detail.setWordWrap(True)
        self.weekly_day_detail.hide()
        history_layout.addWidget(self.weekly_day_detail)
        self.weekly_summary = QLabel(self.history_body)
        self.weekly_summary.setWordWrap(True)
        history_layout.addWidget(self.weekly_summary)
        self.deck_history_title = QLabel(self.history_body)
        _set_font(self.deck_history_title, bold=True)
        history_layout.addWidget(self.deck_history_title)
        self.history_selector = QGridLayout()
        self.history_selector.setContentsMargins(0, 0, 0, 0)
        self.history_selector.setHorizontalSpacing(8)
        self.yesterday = QPushButton(self.history_body)
        self.best = QPushButton(self.history_body)
        for button in (self.yesterday, self.best):
            button.setCheckable(True)
            button.setFlat(True)
        self.history_selector.addWidget(self.yesterday, 0, 0)
        self.history_selector.addWidget(self.best, 0, 1)
        self.history_buttons = QButtonGroup(self)
        self.history_buttons.setExclusive(True)
        self.history_buttons.addButton(self.yesterday)
        self.history_buttons.addButton(self.best)
        self.yesterday.setChecked(True)
        self.yesterday.clicked.connect(lambda: self._set_history_mode("yesterday"))
        self.best.clicked.connect(lambda: self._set_history_mode("best"))
        history_layout.addLayout(self.history_selector)
        self._history_compact = False
        self.history_summary = QLabel(self.history_body)
        self.history_summary.setWordWrap(True)
        history_layout.addWidget(self.history_summary)
        self.history_body.hide()
        outer.addWidget(self.history_body)

        self.error_box = QWidget(self)
        error_layout = QHBoxLayout(self.error_box)
        error_layout.setContentsMargins(0, 2, 0, 0)
        self.error_text = QLabel(self.error_box)
        self.error_text.setWordWrap(True)
        self.retry = QPushButton(self.error_box)
        self.retry.clicked.connect(self._handle_error_action)
        error_layout.addWidget(self.error_text, 1)
        error_layout.addWidget(self.retry)
        self.error_box.hide()
        outer.addWidget(self.error_box)

        self.refresh()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_responsive_layout()

    def _apply_responsive_layout(self) -> None:
        available = max(0, self.content.width() - 28)
        own_needed = (
            max(self.own_time.sizeHint().width(), self.time_caption.sizeHint().width())
            + max(
                self.own_answers.sizeHint().width(),
                self.answer_caption.sizeHint().width(),
            )
            + self.own_values.horizontalSpacing()
        )
        own_compact = own_needed > available
        if own_compact != self._own_compact:
            self._own_compact = own_compact
            for widget in (
                self.own_time,
                self.own_answers,
                self.time_caption,
                self.answer_caption,
            ):
                self.own_values.removeWidget(widget)
            if own_compact:
                self.own_values.addWidget(self.own_time, 0, 0)
                self.own_values.addWidget(self.time_caption, 1, 0)
                self.own_values.addWidget(self.own_answers, 2, 0)
                self.own_values.addWidget(self.answer_caption, 3, 0)
            else:
                self.own_values.addWidget(self.own_time, 0, 0)
                self.own_values.addWidget(self.own_answers, 0, 1)
                self.own_values.addWidget(self.time_caption, 1, 0)
                self.own_values.addWidget(self.answer_caption, 1, 1)

        history_needed = (
            self.yesterday.sizeHint().width()
            + self.best.sizeHint().width()
            + self.history_selector.horizontalSpacing()
        )
        history_compact = history_needed > available
        if history_compact != self._history_compact:
            self._history_compact = history_compact
            self.history_selector.removeWidget(self.yesterday)
            self.history_selector.removeWidget(self.best)
            if history_compact:
                self.history_selector.addWidget(self.yesterday, 0, 0)
                self.history_selector.addWidget(self.best, 1, 0)
            else:
                self.history_selector.addWidget(self.yesterday, 0, 0)
                self.history_selector.addWidget(self.best, 0, 1)

    def _request_collapsed(self, collapsed: bool) -> None:
        setter = getattr(self.controller, "set_panel_collapsed", None)
        if callable(setter):
            setter(collapsed)
        else:
            self.set_collapsed(collapsed)

    def set_collapsed(self, collapsed: bool) -> None:
        self.collapsed = bool(collapsed)
        self.content.setVisible(not self.collapsed)
        self.expand_panel.setVisible(self.collapsed)
        self.setMinimumWidth(36 if self.collapsed else 280)
        self.setMaximumWidth(52 if self.collapsed else 16777215)
        self.update_collapse_controls()

    def update_collapse_controls(self) -> None:
        collapse_text = self.tr("패널 접기", "Collapse panel")
        expand_text = self.tr("패널 펼치기", "Expand panel")
        self.collapse_panel.setToolTip(collapse_text)
        self.collapse_panel.setAccessibleName(collapse_text)
        self.expand_panel.setToolTip(expand_text)
        self.expand_panel.setAccessibleName(expand_text)

    def tr(self, ko: str, en: str) -> str:
        translate = getattr(self.controller, "t", None)
        if callable(translate):
            return translate(ko, en)
        return en if getattr(self.controller, "locale", "ko") == "en" else ko

    def _separator(self) -> QFrame:
        line = QFrame(self)
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        return line

    def active_green(self) -> str:
        color = self.palette().color(self.backgroundRole())
        lightness = (color.red() * 299 + color.green() * 587 + color.blue() * 114) / 1000
        return "#32734e" if lightness > 128 else "#83bb99"

    def status_text(self, status: str) -> str:
        labels = {
            "studying": ("공부 중", "Studying"),
            "paused": ("잠시 멈춤", "Paused"),
            "online": ("접속 중", "Online"),
            "offline": ("마지막 기록", "Last seen"),
        }
        ko, en = labels.get(status, labels["offline"])
        return self.tr(ko, en)

    def update_status_dot(self, dot: QLabel, status: str) -> None:
        dot.setText("●" if status == "studying" else "○")
        dot.setStyleSheet(f"color: {self.active_green()};" if status == "studying" else "")
        dot.setAccessibleName(self.status_text(status))
        dot.setVisible(status in ("studying", "online"))

    def member_name(self, member: dict) -> str:
        display = getattr(self.controller, "display_member_name", None)
        if callable(display):
            return str(display(member))
        return str(member.get("display_name") or self.tr("친구", "Friend"))

    def format_duration(self, seconds: int) -> str:
        return self.format_clock(seconds)

    def format_clock(self, seconds: int) -> str:
        minutes, remaining = divmod(max(0, int(seconds)), 60)
        if minutes >= 60:
            hours, minutes = divmod(minutes, 60)
            return f"{hours}:{minutes:02d}:{remaining:02d}"
        return f"{minutes:02d}:{remaining:02d}"

    def set_metric(self, label: QLabel, value: str, goal: str | None) -> None:
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setAccessibleName(f"{value} / {goal}" if goal else value)
        text = html.escape(value)
        if goal:
            foreground = self.palette().color(QPalette.ColorRole.WindowText)
            background = self.palette().color(QPalette.ColorRole.Window)
            channels = [round(a * 0.7 + b * 0.3) for a, b in zip(
                foreground.getRgb()[:3], background.getRgb()[:3]
            )]
            color = "#%02x%02x%02x" % tuple(channels)
            size = max(8, label.font().pointSizeF() * 0.72)
            text += f'<span style="font-size: {size:.1f}pt; font-weight: 400; color: {color}"> / {html.escape(goal)}</span>'
        label.setText(text)

    def updated_time(self, value) -> str:
        if not value:
            return self.tr("확인 전", "Not checked")
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.astimezone(TIMEZONE).strftime("%H:%M")
        except (TypeError, ValueError):
            return self.tr("확인 전", "Not checked")

    def _toggle_history(self, checked: bool) -> None:
        self.history_body.setVisible(checked)
        self._update_history_toggle()
        if checked:
            self._refresh_history(_now())

    def _handle_error_action(self) -> None:
        if self.controller.online.get("review_error"):
            self.controller.review_dirty = True
            self.controller.review_upload_requested = True
            self.controller.refresh_review_history()
            return
        if self.controller.online.get("recovery_notice") and not self.controller.online.get("last_error"):
            self.controller.online.pop("recovery_notice", None)
            self.controller.save()
            self.refresh()
            return
        self.controller.sync_async(force=True)

    def _set_history_mode(self, mode: str) -> None:
        self.history_mode = mode
        self._refresh_history(_now())

    def _update_history_toggle(self) -> None:
        state = self.tr("닫기", "Hide") if self.history_toggle.isChecked() else self.tr("보기", "Show")
        self.history_toggle.setText(self.tr(f"내 기록    {state}", f"My history    {state}"))
        self.history_toggle.setToolTip(self.tr(
            "최근 7일: 이 PC에 동기화된 Anki 복습 기록\n덱 비교: 이 PC의 활동 시간 · 모바일 기록 제외",
            "Recent 7 days: Anki reviews synced to this PC\nDeck comparison: activity on this PC · excludes mobile reviews",
        ))

    def _comparison_value(self, result: dict | None, *names):
        if not result:
            return None
        reference = result.get("reference") or {}
        for name in names:
            if name in result:
                return result[name]
            if name in reference:
                return reference[name]
        return None

    def _refresh_history(self, current: datetime) -> None:
        self._refresh_weekly(current)
        self.deck_history_title.setText(self.tr("현재 덱 비교", "Current deck comparison"))
        tracker = self.controller.tracker
        deck_id = tracker.current_deck_id
        deck_record = tracker.today_deck(current)
        if not deck_id or not deck_record:
            self.history_summary.setText(
                self.tr("복습할 덱을 선택하면 표시됩니다.", "Select a deck to see its history.")
            )
            return

        deck_name = html.escape(
            str(
                deck_record.get("name")
                or tracker.current_deck_name
                or self.tr("현재 덱", "Current deck")
            )
        )
        today_seconds = float(deck_record.get("seconds") or 0)
        today_answers = int(deck_record.get("answers") or 0)
        today_rate = answers_per_minute(today_seconds, today_answers)
        today_rate_text = self._rate_text(today_rate)

        result = get_comparison(
            tracker.deck_records, str(deck_id), current, mode=self.history_mode
        )
        reference_rate = self._comparison_value(result, "reference_rate", "rate")
        difference = self._comparison_value(
            result,
            "percent_change",
            "difference_percent",
            "percent_difference",
            "change_percent",
        )
        date_value = self._comparison_value(result, "reference_date", "date", "day")
        comparable = self._comparison_value(result, "comparable", "valid")
        if comparable is None:
            comparable = difference is not None

        mode_label = (
            self.tr("어제", "Yesterday")
            if self.history_mode == "yesterday"
            else self.tr("30일 최고", "30-day best")
        )
        if date_value:
            try:
                date_label = datetime.fromisoformat(str(date_value)).strftime("%m/%d")
            except ValueError:
                date_label = str(date_value)
            mode_label = f"{mode_label} · {date_label}"

        if not result or result.get("reference") is None:
            reference_text = self.tr("기록 없음", "No record")
            difference_text = "—"
        else:
            reference_text = self._rate_text(reference_rate)
            if comparable and difference is not None:
                difference_text = f"{float(difference):+.0f}%"
            else:
                difference_text = self.tr("기록 부족", "Not enough data")

        self.history_summary.setText(
            f"<b>{deck_name}</b><br>"
            f"{self.tr('오늘 평균', 'Today average')}    {today_rate_text}<br>"
            f"{mode_label}    {reference_text}<br>"
            f"{self.tr('평균 속도 차이', 'Average pace difference')}    {difference_text}"
        )

    def _refresh_weekly(self, current: datetime) -> None:
        self.weekly_title.setText(self.tr("최근 7일", "Recent 7 days"))
        self.weekly_title.setToolTip(self.tr(
            "이 PC에 동기화된 Anki 복습 기록 · 한국 시간(UTC+9)",
            "Anki review history synced to this PC · UTC+9",
        ))
        getter = getattr(self.controller, "weekly_record", None)
        record = getter(current) if callable(getter) else None
        days = list((record or {}).get("days") or [])
        valid = len(days) == 7
        self.weekly_bars.setVisible(valid)
        if not valid:
            self.weekly_day_detail.hide()
            self.weekly_summary.setText(
                self.tr("주간 기록을 확인할 수 없습니다.", "Weekly history unavailable.")
            )
            return

        maximum = max(1, *(max(0, int(day.get("answers") or 0)) for day in days))
        today = current.date().isoformat()
        for button, day in zip(self.weekly_days, days):
            button.set_day(day, maximum, today)

        selected = next(
            (button for button in self.weekly_days if button.day == self.weekly_selected_day),
            None,
        )
        if selected:
            self.weekly_day_detail.setText(selected.accessibleName())
            self.weekly_day_detail.show()
        else:
            self.weekly_day_detail.hide()

        active_days = max(0, int(record.get("active_days") or 0))
        answers = max(0, int(record.get("answers") or 0))
        seconds = max(0, int(record.get("seconds") or 0))
        previous_answers = max(0, int(record.get("previous_answers") or 0))
        previous_seconds = max(0, int(record.get("previous_seconds") or 0))
        answer_diff = answers - previous_answers
        time_diff = seconds - previous_seconds

        def signed_count(value: int) -> str:
            return f"{value:+d}" if value else "±0"

        def signed_time(value: int) -> str:
            prefix = "+" if value > 0 else "−" if value < 0 else "±"
            return prefix + self.format_clock(abs(value))

        self.weekly_summary.setText(self.tr(
            f"{active_days}일 · {self.format_clock(seconds)} · {answers}회\n"
            f"이전 7일보다  시간 {signed_time(time_diff)} · 답변 {signed_count(answer_diff)}",
            f"{active_days} days · {self.format_clock(seconds)} · {answers} answers\n"
            f"vs previous 7 days  time {signed_time(time_diff)} · answers {signed_count(answer_diff)}",
        ))
        checked = ""
        if record.get("as_of"):
            try:
                parsed = datetime.fromisoformat(str(record["as_of"]).replace("Z", "+00:00"))
                checked = parsed.astimezone(TIMEZONE).strftime("%m/%d %H:%M")
            except (TypeError, ValueError):
                pass
        cutoff = self.tr(
            f"마지막 확인 {checked} (UTC+9)" if checked else "마지막 확인 시각(UTC+9)",
            f"Last checked {checked} (UTC+9)" if checked else "Last checked time (UTC+9)",
        )
        self.weekly_summary.setToolTip(self.tr(
            f"{cutoff} 기준으로 최근 7일과 이전 7일을 비교합니다.",
            f"Compares recent and previous 7-day periods at {cutoff}.",
        ))

    def show_weekly_day(self, button: WeeklyDayButton) -> None:
        self.weekly_selected_day = button.day
        self.weekly_day_detail.setText(button.accessibleName())
        self.weekly_day_detail.setVisible(True)

    def _rate_text(self, value) -> str:
        if value is None:
            return "—"
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "—"
        return self.tr(f"{number:.1f}회/분", f"{number:.1f}/min")

    def _member_key(self, member: dict, index: int) -> str:
        return str(
            member.get("user_id")
            or member.get("id")
            or f"{member.get('display_name', 'member')}:{index}"
        )

    def _refresh_members(self, members: list[dict]) -> None:
        incoming: dict[str, dict] = {}
        for index, member in enumerate(members):
            key = self._member_key(member, index)
            incoming[key] = member
            if key not in self.member_rows:
                row = MemberRow(self, member, self.member_body)
                self.member_rows[key] = row
                self.member_order.append(key)
                self.member_layout.insertWidget(self.member_layout.count() - 1, row)

        for key in list(self.member_order):
            row = self.member_rows[key]
            if key in incoming:
                row.update_member(incoming[key])
                row.show()
            else:
                self.member_layout.removeWidget(row)
                row.deleteLater()
                del self.member_rows[key]
                self.member_order.remove(key)

    def _toggle_own_activity(self, expanded: bool) -> None:
        self.own_activity_timeline.setVisible(expanded)
        self._update_own_activity_label()

    def _update_own_activity_label(self) -> None:
        expanded = self.own_activity_toggle.isChecked()
        self.own_activity_toggle.setText(
            self.tr("오늘 시간대 −", "Today's activity −") if expanded
            else self.tr("오늘 시간대 +", "Today's activity +")
        )
        self.own_activity_toggle.setAccessibleName(
            self.tr("오늘 시간대 접기", "Hide today's activity") if expanded
            else self.tr("오늘 시간대 펼치기", "Show today's activity")
        )

    def refresh(self) -> None:
        self.update_collapse_controls()
        current = _now()
        tracker = self.controller.tracker
        record = self.controller.study_record(current)
        group = self.controller.online.get("group")
        raw_members = self.controller.online.get("members")
        my_id = (self.controller.online.get("auth") or {}).get("user_id")

        if group:
            room_name = str(group.get("name") or self.tr("스터디방", "Study room"))
            self.room_name.setText(_allow_anywhere_wrap(room_name))
            self.room_name.setToolTip(room_name)
            members_known = raw_members is not None
            members = [
                member
                for member in (raw_members or [])
                if member.get("user_id") != my_id
            ]
            studying_count = (1 if tracker.status == "studying" else 0) + sum(
                self.controller._current_member_status(member) == "studying"
                for member in members
            )
            if members_known:
                if tracker.status == "studying":
                    presence = self.tr(
                        f"{studying_count}명 공부 중 · 나 포함",
                        f"{studying_count} studying · including you",
                    )
                else:
                    presence = self.tr(
                        f"{studying_count}명 공부 중", f"{studying_count} studying"
                    )
                self.room_presence.setText(presence)
            else:
                self.room_presence.setText(self.tr("기록 확인 중", "Checking records"))
        else:
            self.room_name.setText(self.tr("스터디방", "Study room"))
            self.room_name.setToolTip("")
            self.room_presence.setText(self.tr("아직 참여한 방이 없습니다", "Not in a room yet"))
            members_known = True
            members = []

        self.manage.setText(self.tr("관리", "Manage"))
        self.manage.setAccessibleName(self.tr("스터디방 관리", "Manage study room"))

        self.own_title.setText(
            self.tr("나 · 오늘", "You · today")
        )
        my_total = next((member for member in (raw_members or [])
                         if member.get("user_id") == my_id
                         and member.get("study_day") == current.date().isoformat()), None)
        self._update_own_activity_label()
        self.own_activity_toggle.setVisible(bool(group))
        self.own_activity_timeline.setVisible(
            bool(group) and self.own_activity_toggle.isChecked()
        )
        self.own_activity_timeline.update_activity(my_total or {})
        self.own_title.setToolTip(
            self.tr(
                "Anki 복습 기록 기준 · 한국 시간 자정(UTC+9)\n모바일 기록은 모바일과 PC의 Anki 동기화 후 반영됩니다.\n시간은 Anki가 저장한 답변 시간이며 실행 중인 타이머가 아닙니다.",
                "Anki review history · resets at midnight UTC+9\nMobile reviews appear after syncing Anki on mobile and PC.\nTime is recorded answer time, not a running stopwatch.",
            )
        )
        own_status = self.status_text(
            "online" if tracker.status == "stopped" else tracker.status
        )
        self.own_status.setText(own_status)
        self.update_status_dot(
            self.own_dot, "online" if tracker.status == "stopped" else tracker.status
        )

        own_record = my_total if group else record
        seconds = max(0, int((own_record or {}).get("active_seconds" if group else "seconds") or 0))
        time_value = self.format_clock(seconds)
        time_goal = max(0, int(tracker.time_goal_minutes or 0))
        answer_value = max(0, int((own_record or {}).get("answer_count" if group else "answers") or 0))
        answer_goal = max(0, int(tracker.card_goal or 0))
        pending = bool(group and not my_total)
        self.set_metric(self.own_time, "—" if pending else time_value,
                        self.format_clock(time_goal * 60) if time_goal and not pending else None)
        self.set_metric(self.own_answers, "—" if pending else str(answer_value),
                        str(answer_goal) if answer_goal and not pending else None)
        self.time_caption.setText(
            self.tr("동기화 대기", "Awaiting sync") if pending
            else self.tr("공부 시간", "Study time")
        )
        self.answer_caption.setText(self.tr("답변", "Answers"))

        self.people_caption.setText(self.tr("친구", "Friends"))
        self.time_column.setText(self.tr("시간", "Time"))
        self.answer_column.setText(self.tr("답변", "Answers"))
        self._refresh_members(members)
        self.member_scroll.setVisible(bool(members))
        self.member_empty.setVisible(not members)
        if members:
            self.member_empty.clear()
        elif not group:
            self.member_empty.setText(
                self.tr("방을 만들거나 초대 코드로 참가하세요.", "Create a room or join with an invite code.")
            )
        elif not members_known:
            self.member_empty.setText(self.tr("친구 기록 확인 중", "Checking friend records"))
        else:
            self.member_empty.setText(
                self.tr("아직 친구 기록이 없습니다.", "No friend records yet.")
            )

        self.yesterday.setText(self.tr("어제", "Yesterday"))
        self.best.setText(self.tr("30일 최고", "30-day best"))
        self._update_history_toggle()
        if self.history_toggle.isChecked():
            self._refresh_history(current)

        retryable = self.controller.online.get("review_error") or self.controller.online.get("last_error")
        error = retryable or self.controller.online.get("recovery_notice")
        self.error_box.setVisible(bool(error))
        if error:
            self.error_text.setText(
                self.tr("동기화 지연", "Sync delayed") if retryable
                else str(error)
            )
            self.error_text.setToolTip(str(error))
            self.retry.setText(
                self.tr("재시도", "Retry") if retryable
                else self.tr("확인", "Dismiss")
            )
            self.retry.setVisible(True)
        self._apply_responsive_layout()
