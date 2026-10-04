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
    QLineEdit,
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
from .study_day import (
    DAY_START_HOUR,
    room_datetime,
    room_time_zone as get_room_time_zone,
    study_day as room_study_day,
)
from .tracker import TIMEZONE, answers_per_minute
from .ux_services import ANSWER_GOAL_MAX, TIME_GOAL_MAX_MINUTES


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


class GoalMetric(QLabel):
    """Rich-text metric that behaves like a quiet, accessible button."""

    def __init__(self, activated, parent=None):
        super().__init__(parent)
        self._activated = activated
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "QLabel { padding: 3px 4px; border: 1px solid transparent; border-radius: 4px; }"
            "QLabel:hover { background: palette(midlight); }"
            "QLabel:focus { border-color: palette(highlight); }"
        )

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._activated()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self._activated()
            event.accept()
            return
        super().keyPressEvent(event)


class GoalEditor(QFrame):
    """Single-goal popup; refreshes behind it never replace the user's draft."""

    def __init__(self, panel: "StudyPanel", kind: str, anchor: QWidget):
        super().__init__(panel, Qt.WindowType.Popup)
        self.panel = panel
        self.kind = kind
        self.anchor = anchor
        self.maximum = TIME_GOAL_MAX_MINUTES if kind == "time" else ANSWER_GOAL_MAX
        current = (
            panel.controller.tracker.time_goal_minutes
            if kind == "time"
            else panel.controller.tracker.card_goal
        )
        self.current = max(0, int(current or 0))
        self.setObjectName("goal_editor")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            "QFrame#goal_editor { background: palette(window); border: 1px solid palette(mid); border-radius: 5px; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(6)
        title = QLabel(
            panel.tr("공부 시간 목표", "Study time goal")
            if kind == "time"
            else panel.tr("답변 목표", "Answer goal"),
            self,
        )
        _set_font(title, bold=True)
        layout.addWidget(title)

        entry_row = QHBoxLayout()
        self.input = QLineEdit(self)
        self.input.setText(str(self.current) if self.current else "")
        self.input.setMaxLength(6)
        self.input.setMaximumWidth(120)
        self.input.setAccessibleName(title.text())
        self.input.setPlaceholderText("60" if kind == "time" else "100")
        self.unit = QLabel(panel.tr("분", "min") if kind == "time" else panel.tr("회", "answers"), self)
        entry_row.addWidget(self.input, 1)
        entry_row.addWidget(self.unit)
        layout.addLayout(entry_row)

        self.error = QLabel(self)
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)

        buttons = QHBoxLayout()
        self.clear = QPushButton(panel.tr("목표 해제", "Clear goal"), self)
        self.cancel = QPushButton(panel.tr("취소", "Cancel"), self)
        self.save = QPushButton(panel.tr("저장", "Save"), self)
        self.save.setDefault(True)
        self.clear.setVisible(bool(self.current))
        buttons.addWidget(self.clear)
        buttons.addStretch()
        buttons.addWidget(self.cancel)
        buttons.addWidget(self.save)
        layout.addLayout(buttons)

        self.input.textChanged.connect(self._validate)
        self.input.returnPressed.connect(self._save)
        self.clear.clicked.connect(self._clear)
        self.cancel.clicked.connect(self.close)
        self.save.clicked.connect(self._save)
        self._validate()

    def open_near_anchor(self) -> None:
        self.adjustSize()
        self.setMaximumWidth(max(220, self.anchor.screen().availableGeometry().width() - 16))
        self.adjustSize()
        origin = self.anchor.mapToGlobal(self.anchor.rect().bottomLeft())
        screen = self.anchor.screen().availableGeometry()
        x = min(max(screen.left(), origin.x()), screen.right() - self.width())
        y = origin.y() + 4
        if y + self.height() > screen.bottom():
            y = max(screen.top(), self.anchor.mapToGlobal(self.anchor.rect().topLeft()).y() - self.height() - 4)
        self.move(x, y)
        self.show()
        self.raise_()
        self.input.setFocus()
        self.input.selectAll()

    def _value(self):
        text = self.input.text().strip()
        if not re.fullmatch(r"[0-9]+", text):
            return None
        try:
            value = int(text)
        except ValueError:
            return None
        return value if 1 <= value <= self.maximum else None

    def _validate(self) -> None:
        value = self._value()
        self.save.setEnabled(value is not None and value != self.current)
        if not self.input.text().strip() or value is not None:
            self.error.hide()

    def _store(self, value: int) -> None:
        try:
            if self.kind == "time":
                self.panel.controller.update_daily_goals(time_goal_minutes=value)
            else:
                self.panel.controller.update_daily_goals(card_goal=value)
        except Exception as error:
            self.error.setText(str(error) or self.panel.tr("저장하지 못했습니다.", "Could not save."))
            self.error.show()
            self.input.setFocus()
            return
        self.close()

    def _save(self) -> None:
        value = self._value()
        if value is None:
            self.error.setText(self.panel.tr(
                f"1–{self.maximum} 사이의 정수를 입력하세요.",
                f"Enter a whole number from 1 to {self.maximum}.",
            ))
            self.error.show()
            return
        if value != self.current:
            self._store(value)

    def _clear(self) -> None:
        if self.current:
            self._store(0)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        if getattr(self.panel, "goal_editor", None) is self:
            self.panel.goal_editor = None
        super().closeEvent(event)


class PanelToggleButton(QToolButton):
    """Theme-aware edge control with a visible chevron and click target."""

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
        fill.setAlpha(32 if self.isDown() else 20 if self.underMouse() else 0)
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
        self.selected_slot: int | None = None
        self._record_key = None
        self.setMinimumHeight(45)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

    def update_activity(self, member: dict) -> None:
        member_day = member.get("study_day")
        matches_today = (
            member_day is None
            or str(member_day) == self.panel.current_room_day().isoformat()
        )
        self.known = member.get("activity_known") is True and matches_today
        self.error = bool(member.get("activity_error")) and matches_today
        record_key = (
            member.get("user_id"), member_day, self.panel.room_time_zone()
        )
        previous_buckets = self.buckets if record_key == self._record_key else {}
        if record_key != self._record_key:
            self.selected_slot = None
        self._record_key = record_key
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
        if self.error and previous_buckets:
            self.buckets = previous_buckets
            self.known = True
        self.setFixedHeight(max(88, self.fontMetrics().height() * 5) if self.buckets
                            else self.fontMetrics().height() + 8)
        if self.error and not self.buckets:
            self.selected_slot = None
        elif self.selected_slot not in self.buckets:
            self.selected_slot = max(self.buckets, default=None)
        self.setAccessibleDescription(
            self._description(self.selected_slot) if self.selected_slot is not None else ""
        )
        descriptions = [self._description(slot) for slot in sorted(self.buckets)]
        if self.error:
            summary = self.panel.tr("시간대 동기화 지연", "Activity sync delayed")
        elif not self.known:
            summary = self.panel.tr("시간대 기록 없음", "Timeline unavailable")
        elif not descriptions:
            summary = self.panel.tr("오늘 답변 기록 없음", "No answers recorded today")
        else:
            summary = self.panel.tr("오늘 활동: ", "Today's activity: ") + "; ".join(descriptions)
        summary += f" · {self.panel.room_day_label()}"
        self.setAccessibleName(summary)
        self.setToolTip(summary)
        self.update()

    def _description(self, slot: int) -> str:
        bucket = self.buckets[slot]
        start_minutes = (DAY_START_HOUR * 60 + slot * 15) % (24 * 60)
        end_minutes = (start_minutes + 15) % (24 * 60)
        start = f"{start_minutes // 60:02d}:{start_minutes % 60:02d}"
        end = f"{end_minutes // 60:02d}:{end_minutes % 60:02d}"
        answers = bucket["answer_count"]
        duration = self.panel.format_clock(round(bucket["time_ms"] / 1000))
        time_zone = self.panel.room_time_zone()
        return self.panel.tr(
            f"{start}–{end} ({time_zone}) · {answers}회 · 기록 시간 {duration}",
            f"{start}–{end} ({time_zone}) · {answers} answers · recorded time {duration}",
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

    def _select_slot(self, slot: int) -> None:
        if self.error or slot not in self.buckets:
            return
        self.selected_slot = slot
        self.setAccessibleDescription(self._description(slot))
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.buckets and not self.error:
            slot = self._slot_at(int(event.position().x()))
            if slot is not None:
                closest = min(self.buckets, key=lambda candidate: abs(candidate - slot))
                if abs(closest - slot) <= 2:
                    self._select_slot(closest)
                    self.setFocus()
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right) and self.buckets and not self.error:
            slots = sorted(self.buckets)
            index = slots.index(self.selected_slot) if self.selected_slot in slots else 0
            step = 1 if event.key() == Qt.Key.Key_Right else -1
            self._select_slot(slots[max(0, min(len(slots) - 1, index + step))])
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        foreground = self.palette().color(QPalette.ColorRole.WindowText)
        if not self.buckets:
            painter.setPen(foreground)
            text = self.panel.tr("오늘 기록 없음", "No activity today") if self.known and not self.error else "—"
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
            return
        muted = self.palette().color(QPalette.ColorRole.WindowText)
        muted.setAlpha(170)
        font = painter.font()
        font.setPointSizeF(max(8.0, font.pointSizeF() * 0.85))
        painter.setFont(font)
        metrics = painter.fontMetrics()
        # Reserve a text row with equal padding above and below its glyphs.
        detail_top = self.height() - metrics.height() - 12
        label_y = detail_top - 5
        baseline_y = max(15, label_y - metrics.height() - 4)
        bar_top = metrics.height() + 9
        bar_space = max(10, baseline_y - bar_top)
        if self.known and self.buckets:
            painter.setPen(QPen(muted, 1))
            painter.drawText(1, metrics.ascent() + 1, self.panel.tr("답변 시간 / 15분 구간", "Answer time / 15-min bin"))
            scale = "15:00"
            painter.drawText(self.width() - metrics.horizontalAdvance(scale) - 1, metrics.ascent() + 1, scale)
            painter.drawLine(1, baseline_y, max(1, self.width() - 2), baseline_y)
            width = max(1, self.width() - 2)
            for slot in self.buckets:
                x1 = 1 + round(slot * width / 96)
                x2 = 1 + round((slot + 1) * width / 96)
                height = max(2, round(bar_space * min(1, self.buckets[slot]["time_ms"] / 900000)))
                selected = slot == self.selected_slot
                if selected:
                    shade = self.palette().color(QPalette.ColorRole.WindowText)
                    shade.setAlpha(25)
                    painter.fillRect(x1 - 2, bar_top, max(5, x2 - x1 + 3), bar_space, shade)
                painter.fillRect(x1, baseline_y - height, max(1, x2 - x1 - 1), height, foreground if selected else muted)

            if self.selected_slot in self.buckets:
                slot = self.selected_slot
                bucket = self.buckets[slot]
                start = (DAY_START_HOUR * 60 + slot * 15) % (24 * 60)
                end = (start + 15) % (24 * 60)
                period = f"{start // 60:02d}:{start % 60:02d}–{end // 60:02d}:{end % 60:02d}"
                duration = self.panel.format_clock(round(bucket["time_ms"] / 1000))
                answers = bucket["answer_count"]
                detail = self.panel.tr(f"{duration} · {answers}회", f"{duration} · {answers} answers")
                painter.drawLine(1, detail_top, self.width() - 1, detail_top)
                painter.setPen(foreground)
                text_y = detail_top + 6 + metrics.ascent()
                painter.drawText(1, text_y, period)
                painter.drawText(self.width() - metrics.horizontalAdvance(detail) - 1, text_y, detail)

        if not self.known or not self.buckets:
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
        labels = ((0, "04"), (24, "10"), (48, "16"), (72, "22"), (96, "04"))
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
        layout.setContentsMargins(0, 5, 0, 5)
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

        self.deck = QLabel(self.identity)
        self.deck.setTextFormat(Qt.TextFormat.PlainText)
        self.deck.setWordWrap(True)
        self.deck.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.summary.addWidget(self.deck, 1, 1, 1, 3)

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
        self.compact_metrics = QLabel(self.identity)
        self.compact_metrics.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.compact_metrics.hide()
        for label in (self.dot, self.time, self.answers):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.identity)

        self.detail_body = QWidget(self)
        detail_layout = QVBoxLayout(self.detail_body)
        detail_layout.setContentsMargins(18, 0, 0, 0)
        detail_layout.setSpacing(4)
        self.details = QLabel(self.detail_body)
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setWordWrap(True)
        detail_layout.addWidget(self.details)
        self.activity_timeline = ActivityTimeline(panel, self.detail_body)
        detail_layout.addWidget(self.activity_timeline)
        self.activity_retry = QPushButton(self.detail_body)
        self.activity_retry.setFlat(True)
        self.activity_retry.clicked.connect(lambda: self.panel.controller.sync_async(force=True))
        detail_layout.addWidget(self.activity_retry)
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
        rows = getattr(self.panel, "member_rows", {})
        show_columns = not compact and not any(row._compact for row in rows.values() if row is not self)
        self.panel.time_column.setVisible(show_columns)
        self.panel.answer_column.setVisible(show_columns)
        if compact == self._compact:
            return
        self._compact = compact
        self.summary.removeWidget(self.dot)
        self.summary.removeWidget(self.identity_text)
        self.summary.removeWidget(self.time)
        self.summary.removeWidget(self.answers)
        self.summary.removeWidget(self.compact_metrics)
        self.summary.removeWidget(self.deck)
        self.time.setVisible(not compact)
        self.answers.setVisible(not compact)
        self.compact_metrics.setVisible(compact)
        if compact:
            self.summary.addWidget(self.dot, 0, 0)
            self.summary.addWidget(self.identity_text, 0, 1, 1, 3)
            self.summary.addWidget(self.deck, 1, 1, 1, 3)
            self.summary.addWidget(self.compact_metrics, 2, 1, 1, 3)
        else:
            self.summary.addWidget(self.dot, 0, 0)
            self.summary.addWidget(self.identity_text, 0, 1)
            self.summary.addWidget(self.time, 0, 2)
            self.summary.addWidget(self.answers, 0, 3)
            self.summary.addWidget(self.deck, 1, 1, 1, 3)

    def toggle_expanded(self) -> None:
        if not self.identity.property("expandable"):
            return
        self.expanded = not self.expanded
        self.identity.setChecked(self.expanded)
        self.detail_body.setVisible(self.expanded)
        self.update_member(self.member)

    def update_member(self, member: dict) -> None:
        self.member = member
        foreground = self.panel.palette().color(QPalette.ColorRole.WindowText).name()
        for label in (self.identity_text, self.time, self.answers):
            label.setStyleSheet(f"color: {foreground}; background: transparent;")
        muted = self.panel.palette().color(QPalette.ColorRole.PlaceholderText).name()
        self.deck.setStyleSheet(f"color: {muted}; background: transparent;")
        status = self.panel.controller._current_member_status(member)
        name = self.panel.member_name(member)
        status_text = self.panel.status_text(status) if status in ("studying", "paused", "online") else ""
        status_suffix = f"  ·  {status_text}" if status_text else ""
        self.identity_text.setText(
            f"{_allow_anywhere_wrap(name)}{status_suffix}  {'-' if self.expanded else '+'}"
        )
        self.identity.setToolTip(f"{name} · {status_text}" if status_text else name)
        self.panel.update_status_dot(self.dot, status)

        seconds = max(0, int(member.get("active_seconds") or 0))
        answers = max(0, int(member.get("answer_count") or 0))
        self.time.setText(self.panel.format_duration(seconds) if member.get("active_seconds") is not None else "—")
        self.answers.setText(str(answers) if member.get("answer_count") is not None else "—")
        self.compact_metrics.setText(self.panel.tr(
            f"{self.time.text()} · {self.answers.text()}회",
            f"{self.time.text()} · {self.answers.text()} answers",
        ))
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
        self.deck.setText(deck_name or "")
        self.deck.setVisible(bool(deck_name))
        self.deck.setAccessibleName(
            self.panel.tr(f"공부 중인 덱 {deck_name}", f"Current deck {deck_name}")
            if deck_name else ""
        )
        self.identity.setToolTip(
            " · ".join(part for part in (name, status_text, deck_name) if part)
        )
        lines = []
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
        timeline = self.activity_timeline
        self.activity_retry.setText(self.panel.tr("재시도", "Retry"))
        self.activity_retry.setVisible(timeline.error)
        timeline.setVisible(bool(timeline.buckets) or not timeline.error)
        expandable = bool(lines or timeline.known or timeline.buckets or timeline.error)
        changed = self.identity.property("expandable") != expandable
        self.identity.setProperty("expandable", expandable)
        self.identity.setCheckable(expandable)
        self.identity.setFocusPolicy(Qt.FocusPolicy.StrongFocus if expandable else Qt.FocusPolicy.NoFocus)
        self.identity.setCursor(Qt.CursorShape.PointingHandCursor if expandable else Qt.CursorShape.ArrowCursor)
        self.identity.setAccessibleName((f"{name}, {status_text}" if status_text else name) + (
            self.panel.tr(". 오늘 활동 보기", ". Show today's activity") if expandable else ""
        ))
        if not expandable:
            self.expanded = False
        sign = f"  {'-' if self.expanded else '+'}" if expandable else ""
        self.identity_text.setText(f"{_allow_anywhere_wrap(name)}{status_suffix}{sign}")
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
        self.goal_editor: GoalEditor | None = None
        self._record_issue: str | None = None

        self.setObjectName("study_companion_body")
        self.setMinimumWidth(280)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.content = QWidget(self)
        root.addWidget(self.content, 1)
        outer = QVBoxLayout(self.content)
        outer.setContentsMargins(14, 10, 14, 10)
        outer.setSpacing(8)

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
        self.room_name.setMaximumHeight(self.room_name.fontMetrics().lineSpacing() * 2)
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
        self.own_time = GoalMetric(lambda: self._open_goal_editor("time"), self)
        _set_font(self.own_time, scale=1.75, bold=True)
        self.own_answers = GoalMetric(lambda: self._open_goal_editor("answers"), self)
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
            "QPushButton { text-align: left; padding: 4px 0; border: none; background: transparent; }"
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

        self.no_room_actions = QWidget(self)
        self.no_room_layout = QGridLayout(self.no_room_actions)
        self.no_room_layout.setContentsMargins(0, 2, 0, 0)
        self.no_room_layout.setHorizontalSpacing(7)
        self.no_room_layout.setVerticalSpacing(6)
        self.create_room = QPushButton(self.no_room_actions)
        self.join_room = QPushButton(self.no_room_actions)
        self.create_room.clicked.connect(lambda: self._show_dialog_page("create"))
        self.join_room.clicked.connect(lambda: self._show_dialog_page("join"))
        self.no_room_layout.addWidget(self.create_room, 0, 0)
        self.no_room_layout.addWidget(self.join_room, 0, 1)
        self.no_room_layout.setColumnStretch(0, 1)
        self.no_room_layout.setColumnStretch(1, 1)
        outer.addWidget(self.no_room_actions)
        self._no_room_compact = False

        self.member_body = QWidget(self)
        self.member_layout = QVBoxLayout(self.member_body)
        self.member_layout.setContentsMargins(0, 0, 0, 0)
        self.member_layout.setSpacing(0)
        self.member_layout.addStretch()
        outer.addWidget(self.member_body)

        outer.addWidget(self._separator())

        self.history_toggle = QPushButton(self)
        self.history_toggle.setFlat(True)
        self.history_toggle.setCheckable(True)
        self.history_toggle.setStyleSheet(
            "QPushButton { text-align: left; padding: 4px 0; font-weight: bold; border: none; background: transparent; }"
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
            f"이 PC에 동기화된 Anki 복습 기록 · {self.room_day_label()}",
            f"Anki review history synced to this PC · {self.room_day_label()}",
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
        self.deck_history_title = QPushButton(self.history_body)
        self.deck_history_title.setCheckable(True)
        self.deck_history_title.setFlat(True)
        self.deck_history_title.setStyleSheet(self.history_toggle.styleSheet())
        history_layout.addWidget(self.deck_history_title)
        self.deck_history_body = QWidget(self.history_body)
        deck_layout = QVBoxLayout(self.deck_history_body)
        deck_layout.setContentsMargins(0, 0, 0, 0)
        self.deck_history_body.hide()
        self.deck_history_title.toggled.connect(self._toggle_deck_history)
        history_layout.addWidget(self.deck_history_body)
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
        deck_layout.addLayout(self.history_selector)
        self._history_compact = False
        self.history_summary = QLabel(self.history_body)
        self.history_summary.setWordWrap(True)
        deck_layout.addWidget(self.history_summary)
        self.history_body.hide()
        outer.addWidget(self.history_body)

        self.error_box = QWidget(self)
        error_layout = QHBoxLayout(self.error_box)
        error_layout.setContentsMargins(0, 2, 0, 0)
        self.error_text = QPushButton(self.error_box)
        self.error_text.setFlat(True)
        self.error_text.setStyleSheet(
            "QPushButton { text-align: left; padding: 3px 0; border: none; background: transparent; }"
            "QPushButton:hover { text-decoration: underline; }"
            "QPushButton:focus { border: 1px solid palette(highlight); border-radius: 3px; }"
        )
        self.error_text.clicked.connect(lambda: self._show_dialog_page("record_status"))
        self.retry = QPushButton(self.error_box)
        self.retry.clicked.connect(self._handle_error_action)
        error_layout.addWidget(self.error_text, 1)
        error_layout.addWidget(self.retry)
        self.error_box.hide()
        outer.addWidget(self.error_box)
        outer.addStretch(1)

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

        no_room_needed = (
            self.create_room.sizeHint().width()
            + self.join_room.sizeHint().width()
            + self.no_room_layout.horizontalSpacing()
        )
        no_room_compact = no_room_needed > available
        if no_room_compact != self._no_room_compact:
            self._no_room_compact = no_room_compact
            self.no_room_layout.removeWidget(self.create_room)
            self.no_room_layout.removeWidget(self.join_room)
            if no_room_compact:
                self.no_room_layout.addWidget(self.create_room, 0, 0)
                self.no_room_layout.addWidget(self.join_room, 1, 0)
            else:
                self.no_room_layout.addWidget(self.create_room, 0, 0)
                self.no_room_layout.addWidget(self.join_room, 0, 1)

    def _request_collapsed(self, collapsed: bool) -> None:
        setter = getattr(self.controller, "set_panel_collapsed", None)
        if callable(setter):
            setter(collapsed)
        else:
            self.set_collapsed(collapsed)

    def _show_dialog_page(self, page: str) -> None:
        self.controller.show_dialog(page=page)

    def _open_goal_editor(self, kind: str) -> None:
        if self.goal_editor is not None:
            if self.goal_editor.kind == kind:
                self.goal_editor.raise_()
                self.goal_editor.input.setFocus()
                return
            self.goal_editor.close()
        anchor = self.own_time if kind == "time" else self.own_answers
        self.goal_editor = GoalEditor(self, kind, anchor)
        self.goal_editor.open_near_anchor()

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

    def room_time_zone(self) -> str:
        return get_room_time_zone(self.controller.online.get("group"))

    def room_day_label(self) -> str:
        return f"{self.room_time_zone()} · {DAY_START_HOUR:02d}:00"

    def current_room_day(self, current: datetime | None = None):
        return room_study_day(current or _now(), self.room_time_zone())

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
        dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
        color = self.active_green() if status == "studying" else (
            "#91b4d8" if dark else "#476f99"
        )
        dot.setText("●")
        dot.setStyleSheet(f"color: {color}; font-size: 8px;")
        dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dot.setAccessibleName(self.status_text(status))
        dot.setToolTip(self.status_text(status))
        dot.setVisible(status in ("studying", "online", "paused"))

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
            return room_datetime(parsed, self.room_time_zone()).strftime("%H:%M")
        except (TypeError, ValueError):
            return self.tr("확인 전", "Not checked")

    def _toggle_history(self, checked: bool) -> None:
        self.history_body.setVisible(checked)
        self._update_history_toggle()
        if checked:
            self._refresh_history(_now())

    def _toggle_deck_history(self, checked: bool) -> None:
        self.deck_history_body.setVisible(checked)
        self._refresh_history(_now())

    def _handle_error_action(self) -> None:
        if self._record_issue:
            self.retry.setEnabled(False)
            if self._record_issue in ("local_read", "local_save"):
                if self._record_issue == "local_read":
                    self.controller.review_dirty = True
                    self.controller.review_upload_requested = True
                    self.controller.refresh_review_history()
                else:
                    try:
                        self.controller.save()
                    except Exception:
                        self.refresh()
                        return
                    self.refresh()
                return
            self.controller.sync_async(force=True)
            return
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

    def _record_status(self) -> dict:
        snapshot = getattr(self.controller, "record_status_snapshot", None)
        if not callable(snapshot):
            return {}
        try:
            return snapshot() or {}
        except Exception:
            return {}

    def _record_issue_text(self, issue: str | None) -> str:
        labels = {
            "local_save": ("기록 저장 실패", "Could not save records"),
            "local_read": ("기록 확인 실패", "Could not read records"),
            "upload": ("공유 지연", "Upload delayed"),
            "members": ("조회 지연", "Refresh delayed"),
            "pending": ("공유 대기", "Pending upload"),
        }
        ko, en = labels.get(issue, ("", ""))
        return self.tr(ko, en)

    def _set_history_mode(self, mode: str) -> None:
        self.history_mode = mode
        self._refresh_history(_now())

    def _update_history_toggle(self) -> None:
        state = "-" if self.history_toggle.isChecked() else "+"
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
        sign = "-" if self.deck_history_title.isChecked() else "+"
        self.deck_history_title.setText(self.tr("현재 덱 비교 ", "Current deck comparison ") + sign)
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
            f"이 PC에 동기화된 Anki 복습 기록 · {self.room_day_label()}",
            f"Anki review history synced to this PC · {self.room_day_label()}",
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
        today = self.current_room_day(current).isoformat()
        for button, day in zip(self.weekly_days, days):
            button.set_day(day, maximum, today)

        selected = next(
            (button for button in self.weekly_days if button.day == self.weekly_selected_day),
            None,
        )
        if selected:
            self.weekly_day_detail.setText(self._weekly_detail(selected))
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

        comparison_text = self.tr(
            f"{active_days}일 · {self.format_clock(seconds)} · {answers}회\n"
            f"이전 7일보다  시간 {signed_time(time_diff)} · 답변 {signed_count(answer_diff)}",
            f"{active_days} days · {self.format_clock(seconds)} · {answers} answers\n"
            f"vs previous 7 days  time {signed_time(time_diff)} · answers {signed_count(answer_diff)}",
        )
        self.weekly_summary.setText(self.tr(
            f"{active_days}/7일 · {self.format_clock(seconds)} · {answers}회",
            f"{active_days}/7 days · {self.format_clock(seconds)} · {answers} answers",
        ))
        checked = ""
        if record.get("as_of"):
            try:
                parsed = datetime.fromisoformat(str(record["as_of"]).replace("Z", "+00:00"))
                checked = room_datetime(parsed, self.room_time_zone()).strftime("%m/%d %H:%M")
            except (TypeError, ValueError):
                pass
        cutoff = self.tr(
            f"마지막 확인 {checked} ({self.room_time_zone()})" if checked
            else f"마지막 확인 시각 ({self.room_time_zone()})",
            f"Last checked {checked} ({self.room_time_zone()})" if checked
            else f"Last checked time ({self.room_time_zone()})",
        )
        self.weekly_summary.setToolTip(comparison_text + "\n" + self.tr(
            f"{cutoff} 기준으로 최근 7일과 이전 7일을 비교합니다.",
            f"Compares recent and previous 7-day periods at {cutoff}.",
        ))

    def show_weekly_day(self, button: WeeklyDayButton) -> None:
        self.weekly_selected_day = button.day
        self.weekly_day_detail.setText(self._weekly_detail(button))
        self.weekly_day_detail.setVisible(True)

    def _weekly_detail(self, button: WeeklyDayButton) -> str:
        day = button.day[5:].replace("-", "/")
        duration = self.format_clock(button.seconds)
        return self.tr(f"{day} · {duration} · {button.answers}회",
                       f"{day} · {duration} · {button.answers} answers")

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
            self.tr("오늘 시간대 -", "Today's activity -") if expanded
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
        review_record = self.controller.study_record(current)
        live_record = tracker.today(current)
        group = self.controller.online.get("group")
        raw_members = self.controller.online.get("members")
        my_id = (self.controller.online.get("auth") or {}).get("user_id")

        if group:
            room_name = str(group.get("name") or self.tr("스터디방", "Study room"))
            self.room_name.setText(_allow_anywhere_wrap(room_name))
            self.room_name.setToolTip(f"{room_name}\n{self.room_day_label()}")
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
                         and member.get("study_day") == self.current_room_day(current).isoformat()), None)
        self._update_own_activity_label()
        self.own_activity_toggle.setVisible(bool(group))
        self.own_activity_timeline.setVisible(
            bool(group) and self.own_activity_toggle.isChecked()
        )
        self.own_activity_timeline.update_activity(my_total or {})
        self.own_title.setToolTip(
            self.tr(
                f"PC 공부 시간은 실시간으로 표시되며 1분 동안 입력이 없으면 멈춥니다.\n{self.room_day_label()}에 새 공부일이 시작됩니다. 모바일 답변은 Anki 동기화 후 반영됩니다.",
                f"PC study time updates live and pauses after 1 minute without input.\nA new study day starts at {self.room_day_label()}. Mobile answers appear after Anki sync.",
            )
        )
        own_status = self.status_text(
            "online" if tracker.status == "stopped" else tracker.status
        )
        self.own_status.setText(own_status)
        self.update_status_dot(
            self.own_dot, "online" if tracker.status == "stopped" else tracker.status
        )

        # Live PC time advances every tick; synced review answers can include mobile.
        seconds = max(0, int(live_record.get("seconds") or 0))
        time_value = self.format_clock(seconds)
        time_goal = max(0, int(tracker.time_goal_minutes or 0))
        answer_value = max(
            0,
            int(review_record.get("answers") or 0),
            int(live_record.get("answers") or 0),
        )
        answer_goal = max(0, int(tracker.card_goal or 0))
        self.set_metric(self.own_time, time_value,
                        self.format_clock(time_goal * 60) if time_goal else None)
        self.set_metric(self.own_answers, str(answer_value),
                        str(answer_goal) if answer_goal else None)
        self.own_time.setToolTip(self.tr("공부 시간 목표 수정", "Edit study time goal"))
        self.own_answers.setToolTip(self.tr("답변 목표 수정", "Edit answer goal"))
        time_metric_name = self.own_time.accessibleName()
        answer_metric_name = self.own_answers.accessibleName()
        self.own_time.setAccessibleName(f"{self.own_time.toolTip()} · {time_metric_name}")
        self.own_answers.setAccessibleName(f"{self.own_answers.toolTip()} · {answer_metric_name}")
        self.time_caption.setText(self.tr("공부 시간", "Study time"))
        self.answer_caption.setText(self.tr("답변", "Answers"))

        self.people_caption.setText(self.tr("친구", "Friends"))
        self.time_column.setText(self.tr("시간", "Time"))
        self.answer_column.setText(self.tr("답변", "Answers"))
        self._refresh_members(members)
        self.member_body.setVisible(bool(members))
        self.member_empty.setVisible(not members)
        self.no_room_actions.setVisible(not group)
        self.create_room.setText(self.tr("방 만들기", "Create room"))
        self.join_room.setText(self.tr("코드로 참여", "Join with a code"))
        if members:
            self.member_empty.clear()
        elif not group:
            self.member_empty.clear()
            self.member_empty.hide()
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

        status = self._record_status()
        self._record_issue = status.get("primary_issue")
        retryable = self.controller.online.get("review_error") or self.controller.online.get("last_error")
        error = self._record_issue or retryable or self.controller.online.get("recovery_notice")
        self.error_box.setVisible(bool(error))
        if error:
            issue_text = self._record_issue_text(self._record_issue)
            self.error_text.setText(
                issue_text or (self.tr("동기화 지연", "Sync delayed") if retryable else str(error))
            )
            issue_detail = (status.get("errors") or {}).get(self._record_issue)
            self.error_text.setToolTip(str(issue_detail or retryable or error))
            self.error_text.setEnabled(bool(self._record_issue))
            self.retry.setText(
                self.tr("재시도", "Retry") if (self._record_issue or retryable)
                else self.tr("확인", "Dismiss")
            )
            self.retry.setVisible(self._record_issue != "pending")
            busy = bool(
                getattr(self.controller, "sync_in_flight", False)
                or getattr(self.controller, "review_query_in_flight", False)
            )
            self.retry.setEnabled(not busy)
        self._apply_responsive_layout()
