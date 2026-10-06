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
    QColor,
    QFontMetrics,
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
    QPointF,
    QToolButton,
    Qt,
    QVBoxLayout,
    QWidget,
)

from .history import get_comparison
from .room_activity import (
    TIE_COLOR,
    member_colors,
    presence_status,
    ranked_places,
    led_counts,
    slot_leader,
    slot_rankings,
    visible_deck_name,
    week_days,
    weekly_room_series,
)
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


def _slot_period(slot: int) -> str:
    start = (DAY_START_HOUR * 60 + slot * 15) % (24 * 60)
    end = (start + 15) % (24 * 60)
    return f"{start // 60:02d}:{start % 60:02d}–{end // 60:02d}:{end % 60:02d}"


def _room_now_fraction(panel: "StudyPanel") -> float:
    """Position of the current room wall time on the 04:00→04:00 axis."""
    local = room_datetime(_now(), panel.room_time_zone())
    minutes = (local.hour * 60 + local.minute - DAY_START_HOUR * 60) % (24 * 60)
    return (minutes + local.second / 60) / (24 * 60)


def _draw_now_line(painter: QPainter, widget: QWidget, panel: "StudyPanel", top: int, bottom: int) -> None:
    """Thin muted marker for the current time; callers draw it only for today."""
    color = widget.palette().color(QPalette.ColorRole.WindowText)
    color.setAlpha(120)
    width = max(1, widget.width() - 2)
    x = 1 + round(_room_now_fraction(panel) * width)
    painter.setPen(QPen(color, 1))
    painter.drawLine(x, top, x, bottom)


def _draw_axis_labels(painter: QPainter, widget: QWidget, label_y: int) -> None:
    metrics = painter.fontMetrics()
    labels = ((0, "04"), (24, "10"), (48, "16"), (72, "22"), (96, "04"))
    width = max(1, widget.width() - 2)
    for slot, label in labels:
        x = 1 + round(slot * width / 96)
        if slot == 96:
            x -= metrics.horizontalAdvance(label)
        elif slot:
            x -= metrics.horizontalAdvance(label) // 2
        painter.drawText(x, label_y, label)


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
        self.is_today = False
        self.buckets: dict[int, dict] = {}
        self.selected_slot: int | None = None
        self._record_key = None
        self._hover_hint = ""
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
        self.is_today = matches_today
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
        # The accessible name lists every bucket for screen readers; the hover
        # tooltip stays short so empty areas do not show that whole list.
        self._hover_hint = self._short_summary()
        self.setToolTip(self._hover_hint)
        self.update()

    def _short_summary(self) -> str:
        if self.error:
            text = self.panel.tr("시간대 동기화 지연", "Activity sync delayed")
        elif not self.known:
            text = self.panel.tr("시간대 기록 없음", "Timeline unavailable")
        elif not self.buckets:
            text = self.panel.tr("오늘 답변 기록 없음", "No answers recorded today")
        else:
            text = self.panel.tr(
                "막대에 마우스를 올려 구간 확인",
                "Hover a bar for bin details",
            )
        return f"{text} · {self.panel.room_day_label()}"

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
            self.setToolTip(self._hover_hint)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self.setToolTip(self._hover_hint)
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
            if self.is_today:
                _draw_now_line(painter, self, self.panel, bar_top, baseline_y + 3)
                painter.setPen(QPen(muted, 1))

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
        _draw_axis_labels(painter, self, label_y)

        if self.hasFocus():
            focus = self.palette().color(QPalette.ColorRole.Highlight)
            painter.setPen(QPen(focus, 1))
            painter.drawRect(0, 0, max(0, self.width() - 1), max(0, self.height() - 1))


class RoomTimeline(QWidget):
    """Room-wide 04→04 strip; each 15-minute slot takes its single leader's color."""

    def __init__(self, panel: "StudyPanel", parent=None):
        super().__init__(parent)
        self.panel = panel
        self.rankings: dict[int, list[tuple[str, int]]] = {}
        self.colors: dict[str, str] = {}
        self.names: dict[str, str] = {}
        self.selected_slot: int | None = None
        self.on_select = None
        self._hover_hint = ""
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

    def _label_font(self):
        font = self.font()
        font.setPointSizeF(max(8.0, font.pointSizeF() * 0.85))
        return font

    def _strip_height(self) -> int:
        return max(12, round(self.fontMetrics().height() * 0.9))

    def update_room(self, rankings: dict, colors: dict, names: dict) -> None:
        self.rankings = dict(rankings)
        self.colors = dict(colors)
        self.names = dict(names)
        if self.selected_slot not in self.rankings:
            self.selected_slot = max(self.rankings, default=None)
        label_height = QFontMetrics(self._label_font()).height()
        self.setFixedHeight(self._strip_height() + 7 + label_height + 2)
        title = self.panel.tr("방 시간대 · 15분 1등", "Room timeline · top per 15 min")
        if self.rankings:
            summary = title + ": " + "; ".join(
                self.describe(slot) for slot in sorted(self.rankings)
            )
            hint = self.panel.tr("구간에 마우스를 올려 1등 확인", "Hover a bin to see who led")
        else:
            empty = self.panel.tr("오늘 답변 기록 없음", "No answers recorded today")
            summary = f"{title}: {empty}"
            hint = empty
        self.setAccessibleName(f"{summary} · {self.panel.room_day_label()}")
        # Like ActivityTimeline, the hover tooltip stays short; only the
        # accessible name carries every bin.
        self._hover_hint = f"{hint} · {self.panel.room_day_label()}"
        self.setToolTip(self._hover_hint)
        self.setAccessibleDescription(self.describe(self.selected_slot))
        self.update()

    def describe(self, slot: int | None) -> str:
        if slot is None or slot not in self.rankings:
            return ""
        parts = [_slot_period(slot)]
        for place, user_id, answers in ranked_places(self.rankings[slot]):
            name = self.names.get(user_id) or self.panel.tr("친구", "Friend")
            parts.append(self.panel.tr(
                f"{place}등 {name} {answers}회",
                f"#{place} {name} {answers}",
            ))
        return " · ".join(parts)

    def _slot_at(self, x: int) -> int | None:
        left, right = 1, max(2, self.width() - 1)
        if x < left or x >= right:
            return None
        return min(95, max(0, int((x - left) * 96 / max(1, right - left))))

    def mouseMoveEvent(self, event) -> None:
        slot = self._slot_at(int(event.position().x()))
        self.setToolTip(self.describe(slot) if slot in self.rankings else self._hover_hint)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self.setToolTip(self._hover_hint)
        super().leaveEvent(event)

    def _select_slot(self, slot: int) -> None:
        if slot not in self.rankings:
            return
        self.selected_slot = slot
        self.setAccessibleDescription(self.describe(slot))
        if callable(self.on_select):
            self.on_select(slot)
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rankings:
            slot = self._slot_at(int(event.position().x()))
            if slot is not None:
                closest = min(self.rankings, key=lambda candidate: abs(candidate - slot))
                if abs(closest - slot) <= 2:
                    self._select_slot(closest)
                    self.setFocus()
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right) and self.rankings:
            slots = sorted(self.rankings)
            index = slots.index(self.selected_slot) if self.selected_slot in slots else 0
            step = 1 if event.key() == Qt.Key.Key_Right else -1
            self._select_slot(slots[max(0, min(len(slots) - 1, index + step))])
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        foreground = self.palette().color(QPalette.ColorRole.WindowText)
        muted = self.palette().color(QPalette.ColorRole.WindowText)
        muted.setAlpha(170)
        track = self.palette().color(QPalette.ColorRole.WindowText)
        track.setAlpha(22)
        strip_top = 3
        strip_height = self._strip_height()
        strip_bottom = strip_top + strip_height
        width = max(1, self.width() - 2)
        painter.fillRect(1, strip_top, width, strip_height, track)
        for slot, ranking in self.rankings.items():
            x1 = 1 + round(slot * width / 96)
            x2 = 1 + round((slot + 1) * width / 96)
            leader = slot_leader(ranking)
            color = QColor(self.colors.get(leader, TIE_COLOR) if leader else TIE_COLOR)
            painter.fillRect(x1, strip_top, max(1, x2 - x1), strip_height, color)
        if self.selected_slot in self.rankings:
            x1 = 1 + round(self.selected_slot * width / 96)
            x2 = 1 + round((self.selected_slot + 1) * width / 96)
            painter.fillRect(x1 - 1, strip_bottom + 1, max(3, x2 - x1 + 2), 2, foreground)
        _draw_now_line(painter, self, self.panel, strip_top - 2, strip_bottom + 2)

        painter.setFont(self._label_font())
        painter.setPen(muted)
        _draw_axis_labels(painter, self, strip_bottom + 5 + painter.fontMetrics().ascent())

        if self.hasFocus():
            focus = self.palette().color(QPalette.ColorRole.Highlight)
            painter.setPen(QPen(focus, 1))
            painter.drawRect(0, 0, max(0, self.width() - 1), max(0, self.height() - 1))


class WeeklyLineChart(QWidget):
    """Recent seven room days as one answer-count line per room member."""

    def __init__(self, panel: "StudyPanel", parent=None):
        super().__init__(parent)
        self.panel = panel
        self.days: list[str] = []
        self.series: list[dict] = []
        self.today = ""
        self.selected_index: int | None = None
        self._hover_hint = ""
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)

    def _small_font(self):
        font = self.font()
        font.setPointSizeF(max(6.5, font.pointSizeF() * 0.72))
        return font

    def set_data(self, days: list[str], series: list[dict], today: str) -> None:
        """``series`` items: key, name, color, me, values (7 ints, None = gap)."""
        self.days = list(days)
        self.series = list(series)
        self.today = today
        self.setFixedHeight(max(96, self.fontMetrics().height() * 6))
        if self.selected_index is not None and self.selected_index >= len(self.days):
            self.selected_index = None
        gaps = any(value is None for item in self.series for value in item["values"])
        title = self.panel.tr("최근 7일 답변 수", "Answers, recent 7 days")
        self.setAccessibleName(title + ": " + "; ".join(
            self.day_description(index) for index in range(len(self.days))
        ))
        hint = self.panel.tr("날짜를 눌러 사람별 답변 확인", "Select a day to compare answers")
        if gaps:
            hint += self.panel.tr("\n끊긴 선: 확인 못 한 날", "\nGaps: days not loaded")
        self._hover_hint = hint
        self.setToolTip(hint)
        if self.selected_index is not None:
            self.setAccessibleDescription(self.day_description(self.selected_index))
        self.update()

    def day_label(self, index: int, *, short: bool = False) -> str:
        try:
            parsed = datetime.fromisoformat(self.days[index])
        except (IndexError, ValueError):
            return "—"
        return str(parsed.day) if short else parsed.strftime("%m/%d")

    def day_description(self, index: int) -> str:
        parts = [self.day_label(index)]
        for item in self.series:
            value = item["values"][index] if index < len(item["values"]) else None
            if value is None:
                parts.append(self.panel.tr(f"{item['name']} 확인 못 함", f"{item['name']} not loaded"))
            else:
                parts.append(self.panel.tr(f"{item['name']} {value}회", f"{item['name']} {value}"))
        text = " · ".join(parts)
        if index < len(self.days) and self.days[index] == self.today:
            text += self.panel.tr(" · 오늘 진행 중", " · today in progress")
        return text

    def _plot_rect(self) -> tuple[int, int, int, int]:
        small = QFontMetrics(self._small_font())
        side = max(12, small.horizontalAdvance("00/00") // 2 + 2)
        top = small.height() + 4
        bottom = self.height() - small.height() - 6
        return side, top, max(side + 1, self.width() - side), max(top + 6, bottom)

    def _x_for(self, index: int) -> int:
        left, _top, right, _bottom = self._plot_rect()
        if len(self.days) <= 1:
            return (left + right) // 2
        return left + round(index * (right - left) / (len(self.days) - 1))

    def _index_at(self, x: int) -> int | None:
        if not self.days:
            return None
        return min(range(len(self.days)), key=lambda index: abs(self._x_for(index) - x))

    def select_index(self, index: int | None) -> None:
        if index is None or not self.days:
            return
        self.selected_index = max(0, min(len(self.days) - 1, index))
        self.setAccessibleDescription(self.day_description(self.selected_index))
        self.panel.show_weekly_day(self.selected_index)
        self.update()

    def mouseMoveEvent(self, event) -> None:
        index = self._index_at(int(event.position().x()))
        self.setToolTip(self.day_description(index) if index is not None else self._hover_hint)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self.setToolTip(self._hover_hint)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.select_index(self._index_at(int(event.position().x())))
            self.setFocus()
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:
        if not self.days:
            super().keyPressEvent(event)
            return
        last = len(self.days) - 1
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            step = 1 if event.key() == Qt.Key.Key_Right else -1
            self.select_index(last if self.selected_index is None else self.selected_index + step)
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.select_index(last if self.selected_index is None else self.selected_index)
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        foreground = self.palette().color(QPalette.ColorRole.WindowText)
        muted = self.palette().color(QPalette.ColorRole.WindowText)
        muted.setAlpha(170)
        grid = self.palette().color(QPalette.ColorRole.WindowText)
        grid.setAlpha(45)
        painter.setFont(self._small_font())
        metrics = painter.fontMetrics()
        left, top, right, bottom = self._plot_rect()
        values = [value for item in self.series for value in item["values"] if value is not None]
        maximum = max([1, *values])

        painter.setPen(QPen(grid, 1))
        painter.drawLine(left, bottom, right, bottom)
        painter.drawLine(left, top, right, top)
        painter.setPen(muted)
        painter.drawText(1, metrics.ascent() + 1, self.panel.tr(f"{maximum}회", f"{maximum}"))

        if self.selected_index is not None and self.days:
            shade = self.palette().color(QPalette.ColorRole.WindowText)
            shade.setAlpha(25)
            x = self._x_for(self.selected_index)
            band = max(10, (right - left) // max(1, len(self.days) * 2))
            painter.fillRect(x - band // 2, top, band, bottom - top, shade)

        def point(index: int, value: int) -> QPointF:
            return QPointF(self._x_for(index), bottom - (bottom - top) * value / maximum)

        # Friends first so the thicker "me" line stays on top.
        ordered = [item for item in self.series if not item.get("me")] + [
            item for item in self.series if item.get("me")
        ]
        for item in ordered:
            color = QColor(item["color"])
            pen = QPen(color, 2.6 if item.get("me") else 1.5)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            item_values = item["values"]
            for index in range(len(item_values) - 1):
                if item_values[index] is None or item_values[index + 1] is None:
                    continue
                painter.drawLine(point(index, item_values[index]), point(index + 1, item_values[index + 1]))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            radius = 3.0 if item.get("me") else 2.2
            for index, value in enumerate(item_values):
                if value is not None:
                    painter.drawEllipse(point(index, value), radius, radius)
            painter.setBrush(Qt.BrushStyle.NoBrush)

        label_y = self.height() - 3 - metrics.descent()
        spacing = (right - left) / max(1, len(self.days) - 1)
        # Narrow panels with large text fall back to day-of-month labels.
        short = metrics.horizontalAdvance("00/00") + 6 > spacing
        for index in range(len(self.days)):
            label = self.day_label(index, short=short)
            painter.setPen(foreground if self.days[index] == self.today else muted)
            x = self._x_for(index) - metrics.horizontalAdvance(label) // 2
            x = max(0, min(self.width() - metrics.horizontalAdvance(label), x))
            painter.drawText(x, label_y, label)

        if self.hasFocus():
            focus = self.palette().color(QPalette.ColorRole.Highlight)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            painter.setPen(QPen(focus, 1))
            painter.drawRect(0, 0, max(0, self.width() - 1), max(0, self.height() - 1))


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
        # The last shared deck stays until it changes, but never one from a
        # previous room day or from a friend who has gone offline.
        deck_name = visible_deck_name(member, status, _now(), self.panel.room_time_zone())
        if deck_name:
            deck_name = _allow_anywhere_wrap(deck_name)
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

        self.room_activity = QWidget(self)
        room_activity_layout = QVBoxLayout(self.room_activity)
        room_activity_layout.setContentsMargins(0, 0, 0, 4)
        room_activity_layout.setSpacing(3)
        self.room_activity_title = QLabel(self.room_activity)
        _set_font(self.room_activity_title, bold=True)
        room_activity_layout.addWidget(self.room_activity_title)
        self.room_timeline = RoomTimeline(self, self.room_activity)
        self.room_timeline.on_select = self._show_room_slot
        room_activity_layout.addWidget(self.room_timeline)
        self.room_timeline_detail = QLabel(self.room_activity)
        self.room_timeline_detail.setTextFormat(Qt.TextFormat.PlainText)
        self.room_timeline_detail.setWordWrap(True)
        room_activity_layout.addWidget(self.room_timeline_detail)
        self.room_timeline_legend = QLabel(self.room_activity)
        self.room_timeline_legend.setTextFormat(Qt.TextFormat.RichText)
        self.room_timeline_legend.setWordWrap(True)
        room_activity_layout.addWidget(self.room_timeline_legend)
        self.room_activity.hide()
        outer.addWidget(self.room_activity)

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
        self.weekly_chart = WeeklyLineChart(self, self.history_body)
        history_layout.addWidget(self.weekly_chart)
        self.weekly_legend = QLabel(self.history_body)
        self.weekly_legend.setTextFormat(Qt.TextFormat.RichText)
        self.weekly_legend.setWordWrap(True)
        self.weekly_legend.hide()
        history_layout.addWidget(self.weekly_legend)
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
        self.weekly_chart.setVisible(valid)
        if not valid:
            self.weekly_day_detail.hide()
            self.weekly_legend.hide()
            self.weekly_summary.setText(
                self.tr("주간 기록을 확인할 수 없습니다.", "Weekly history unavailable.")
            )
            return

        today_date = self.current_room_day(current)
        today = today_date.isoformat()
        self._weekly_days = days
        day_keys = [str(day.get("day") or "") for day in days]
        colors = self._room_member_colors()
        my_id = (self.controller.online.get("auth") or {}).get("user_id")
        series = [{
            "key": my_id or "me",
            "name": self.tr("나", "You"),
            "color": self.my_color(),
            "me": True,
            "values": [max(0, int(day.get("answers") or 0)) for day in days],
        }]
        group = self.controller.online.get("group")
        if group:
            friends = [
                member for member in (self.controller.online.get("members") or [])
                if member.get("user_id") and member.get("user_id") != my_id
            ]
            room_series = weekly_room_series(
                self.controller.online.get("room_week_stats") or {},
                str(group.get("id")), today_date, friends,
            )
            for member in friends:
                by_day = dict(zip(week_days(today_date), room_series[str(member["user_id"])]))
                series.append({
                    "key": str(member["user_id"]),
                    "name": self.member_name(member),
                    "color": colors.get(str(member["user_id"]), TIE_COLOR),
                    "me": False,
                    "values": [by_day.get(day) for day in day_keys],
                })
        self.weekly_chart.set_data(day_keys, series, today)
        self.weekly_legend.setVisible(len(series) > 1)
        if len(series) > 1:
            self.weekly_legend.setText(self._legend_html(
                (item["color"], item["name"], "") for item in series
            ))
            self.weekly_legend.setAccessibleName(
                ", ".join(item["name"] for item in series)
            )

        if self.weekly_selected_day in day_keys:
            self.weekly_chart.selected_index = day_keys.index(self.weekly_selected_day)
            self.weekly_day_detail.setText(self._weekly_detail(self.weekly_chart.selected_index))
            self.weekly_day_detail.show()
        else:
            self.weekly_chart.selected_index = None
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

    def show_weekly_day(self, index: int) -> None:
        chart = self.weekly_chart
        if not 0 <= index < len(chart.days):
            return
        self.weekly_selected_day = chart.days[index]
        self.weekly_day_detail.setText(self._weekly_detail(index))
        self.weekly_day_detail.setVisible(True)

    def _weekly_detail(self, index: int) -> str:
        chart = self.weekly_chart
        day = chart.days[index][5:].replace("-", "/")
        days = getattr(self, "_weekly_days", [])
        seconds = max(0, int(days[index].get("seconds") or 0)) if index < len(days) else 0
        parts = [day, self.format_clock(seconds)]
        for item in chart.series:
            value = item["values"][index]
            if value is None:
                parts.append(f"{item['name']} —")
            elif len(chart.series) == 1:
                parts.append(self.tr(f"{value}회", f"{value} answers"))
            else:
                parts.append(self.tr(f"{item['name']} {value}회", f"{item['name']} {value}"))
        return " · ".join(parts)

    def my_color(self) -> str:
        return self.palette().color(QPalette.ColorRole.Highlight).name()

    def _room_member_colors(self) -> dict[str, str]:
        """Stable member colors by join order; me uses the theme highlight."""
        my_id = (self.controller.online.get("auth") or {}).get("user_id")
        ids = [
            str(member.get("user_id"))
            for member in (self.controller.online.get("members") or [])
            if member.get("user_id")
        ]
        if my_id and str(my_id) not in ids:
            ids.append(str(my_id))
        return member_colors(ids, str(my_id) if my_id else None, self.my_color())

    def _legend_html(self, entries) -> str:
        """Color dot + name (+ suffix) entries that wrap only between entries."""
        items = []
        for color, name, suffix in entries:
            text = html.escape(_allow_anywhere_wrap(str(name)))
            if suffix:
                text += "&nbsp;" + html.escape(suffix).replace(" ", "&nbsp;")
            items.append(f'<span style="color: {color}">●</span>&nbsp;{text}')
        return " &nbsp; ".join(items)

    def _refresh_room_activity(self, current: datetime, group, raw_members, my_id) -> None:
        today = self.current_room_day(current).isoformat()
        todays = [
            member for member in (raw_members or [])
            if member.get("user_id") and member.get("study_day") in (None, today)
        ]
        known = any(member.get("activity_known") is True for member in todays)
        self.room_activity.setVisible(bool(group) and known)
        if not (group and known):
            return
        self.room_activity_title.setText(self.tr("방 시간대 · 15분 1등", "Room timeline · top per 15 min"))
        colors = self._room_member_colors()
        names = {
            str(member["user_id"]): (
                self.tr("나", "You") if member.get("user_id") == my_id else self.member_name(member)
            )
            for member in todays
        }
        rankings = slot_rankings(todays)
        self.room_timeline.update_room(rankings, colors, names)
        self._show_room_slot(self.room_timeline.selected_slot)
        counts = led_counts(rankings)
        active = {user_id for ranking in rankings.values() for user_id, _answers in ranking}
        entries = [
            (
                colors.get(str(member["user_id"]), TIE_COLOR),
                names[str(member["user_id"])],
                self.tr(
                    f"1등 {counts.get(str(member['user_id']), 0)}번",
                    f"1st ×{counts.get(str(member['user_id']), 0)}",
                ),
            )
            for member in todays
            if str(member["user_id"]) in active
        ]
        self.room_timeline_legend.setVisible(bool(entries))
        self.room_timeline_legend.setText(self._legend_html(entries))
        self.room_timeline_legend.setAccessibleName(
            ", ".join(f"{name} {suffix}" for _color, name, suffix in entries)
        )
        self.room_timeline_legend.setToolTip(self.tr(
            "동점인 구간은 회색이며 누구의 1등으로도 세지 않습니다.",
            "Tied bins are gray and count for nobody.",
        ))

    def _show_room_slot(self, slot) -> None:
        text = self.room_timeline.describe(slot)
        self.room_timeline_detail.setText(_allow_anywhere_wrap(
            text or self.tr("오늘 답변 기록 없음", "No answers recorded today")
        ))

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
            # Everyone who is not offline counts; this PC is always connected.
            online_count = 1 + sum(
                self.controller._current_member_status(member) != "offline"
                for member in members
            )
            if members_known:
                self.room_presence.setText(self.tr(
                    f"{online_count}명 접속 중 · 나 포함",
                    f"{online_count} online · including you",
                ))
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
        # Show the same presence friends see: studying for 2 minutes after the
        # last input, even though the study timer itself pauses after 1 minute.
        published = presence_status(
            tracker.status, getattr(tracker, "last_input_at", None), current
        )
        own_status = "online" if published == "stopped" else published
        self.own_status.setText(self.status_text(own_status))
        self.update_status_dot(self.own_dot, own_status)

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

        self._refresh_room_activity(current, group, raw_members, my_id)
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
