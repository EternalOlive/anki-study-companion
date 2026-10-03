"""Native Qt study-room panel used by the Anki add-on.

The panel deliberately owns presentation only.  Tracking, member freshness,
localisation, and network actions stay on the controller.
"""

from __future__ import annotations

import html
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

        summary = QHBoxLayout()
        summary.setContentsMargins(0, 0, 0, 0)
        summary.setSpacing(8)

        self.dot = QLabel(self)
        self.dot.setText("●")
        self.dot.setFixedWidth(10)
        self.dot.setAlignment(
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter
        )
        self.dot.setAccessibleName(self.panel.tr("공부 중", "Studying"))
        summary.addWidget(self.dot)

        self.identity = QPushButton(self)
        self.identity.setFlat(True)
        self.identity.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.identity.setStyleSheet("QPushButton { text-align: left; padding: 2px 0; }")
        self.identity.clicked.connect(self.toggle_expanded)
        summary.addWidget(self.identity, 1)

        self.time = QLabel(self)
        self.time.setMinimumWidth(50)
        self.time.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        summary.addWidget(self.time)

        self.answers = QLabel(self)
        self.answers.setMinimumWidth(44)
        self.answers.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        summary.addWidget(self.answers)
        layout.addLayout(summary)

        self.details = QLabel(self)
        self.details.setWordWrap(True)
        self.details.setContentsMargins(18, 0, 0, 2)
        self.details.hide()
        layout.addWidget(self.details)

        self.update_member(member)

    def toggle_expanded(self) -> None:
        self.expanded = not self.expanded
        self.details.setVisible(self.expanded)

    def update_member(self, member: dict) -> None:
        self.member = member
        status = self.panel.controller._current_member_status(member)
        name = self.panel.member_name(member)
        status_text = self.panel.status_text(status)
        self.identity.setText(f"{name}  ·  {status_text}")
        self.identity.setAccessibleName(
            self.panel.tr(
                f"{name}, {status_text}. 목표와 마지막 기록 보기",
                f"{name}, {status_text}. Show goals and last update",
            )
        )

        self.panel.update_status_dot(self.dot, status)

        seconds = max(0, int(member.get("active_seconds") or 0))
        answers = max(0, int(member.get("answer_count") or 0))
        self.time.setText(self.panel.format_duration(seconds))
        self.answers.setText(self.panel.tr(f"{answers}회", f"{answers}"))
        self.time.setAccessibleName(
            self.panel.tr(f"공부 시간 {self.time.text()}", f"Study time {self.time.text()}")
        )
        self.answers.setAccessibleName(
            self.panel.tr(f"답변 {answers}회", f"{answers} answers")
        )

        time_goal = max(0, int(member.get("time_goal_minutes") or 0))
        answer_goal = max(0, int(member.get("card_goal") or 0))
        time_goal_text = (
            self.panel.tr(f"{time_goal}분", f"{time_goal} min")
            if time_goal
            else self.panel.tr("목표 없음", "No goal")
        )
        answer_goal_text = (
            self.panel.tr(f"{answer_goal}회", f"{answer_goal}")
            if answer_goal
            else self.panel.tr("목표 없음", "No goal")
        )
        updated_text = self.panel.updated_time(member.get("updated_at"))
        self.details.setText(
            self.panel.tr(
                f"시간 목표  {time_goal_text}    답변 목표  {answer_goal_text}\n"
                f"마지막 기록  {updated_text}",
                f"Time goal  {time_goal_text}    Answer goal  {answer_goal_text}\n"
                f"Last update  {updated_text}",
            )
        )


class StudyPanel(QWidget):
    """Compact native panel for the shared study-room experience."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.member_rows: dict[str, MemberRow] = {}
        self.member_order: list[str] = []
        self.history_mode = "yesterday"

        self.setObjectName("study_companion_body")
        self.setMinimumWidth(280)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(8)
        header_text = QVBoxLayout()
        header_text.setSpacing(1)
        self.room_name = QLabel(self)
        _set_font(self.room_name, scale=1.1, bold=True)
        self.room_presence = QLabel(self)
        header_text.addWidget(self.room_name)
        header_text.addWidget(self.room_presence)
        header.addLayout(header_text, 1)
        self.manage = QPushButton(self)
        self.manage.setFlat(True)
        self.manage.clicked.connect(self.controller.show_dialog)
        header.addWidget(self.manage)
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

        own_values = QGridLayout()
        own_values.setContentsMargins(0, 0, 0, 0)
        own_values.setHorizontalSpacing(12)
        own_values.setVerticalSpacing(2)
        self.own_time = QLabel(self)
        _set_font(self.own_time, scale=1.75, bold=True)
        self.own_answers = QLabel(self)
        _set_font(self.own_answers, scale=1.35, bold=True)
        self.time_caption = QLabel(self)
        self.answer_caption = QLabel(self)
        own_values.addWidget(self.own_time, 0, 0)
        own_values.addWidget(self.own_answers, 0, 1)
        own_values.addWidget(self.time_caption, 1, 0)
        own_values.addWidget(self.answer_caption, 1, 1)
        own_values.setColumnStretch(0, 1)
        own_values.setColumnStretch(1, 1)
        outer.addLayout(own_values)

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
        selector = QHBoxLayout()
        self.yesterday = QPushButton(self.history_body)
        self.best = QPushButton(self.history_body)
        for button in (self.yesterday, self.best):
            button.setCheckable(True)
            button.setFlat(True)
            selector.addWidget(button)
        self.history_buttons = QButtonGroup(self)
        self.history_buttons.setExclusive(True)
        self.history_buttons.addButton(self.yesterday)
        self.history_buttons.addButton(self.best)
        self.yesterday.setChecked(True)
        self.yesterday.clicked.connect(lambda: self._set_history_mode("yesterday"))
        self.best.clicked.connect(lambda: self._set_history_mode("best"))
        history_layout.addLayout(selector)
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
        self.retry.clicked.connect(lambda: self.controller.sync_async(force=True))
        error_layout.addWidget(self.error_text, 1)
        error_layout.addWidget(self.retry)
        self.error_box.hide()
        outer.addWidget(self.error_box)

        self.refresh()

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
        minutes = max(0, int(seconds) // 60)
        if minutes >= 60:
            hours, remaining = divmod(minutes, 60)
            return f"{hours}:{remaining:02d}"
        return self.tr(f"{minutes}분", f"{minutes}m")

    def format_clock(self, seconds: int) -> str:
        minutes, remaining = divmod(max(0, int(seconds)), 60)
        return f"{minutes:02d}:{remaining:02d}"

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

    def _set_history_mode(self, mode: str) -> None:
        self.history_mode = mode
        self._refresh_history(_now())

    def _update_history_toggle(self) -> None:
        state = self.tr("닫기", "Hide") if self.history_toggle.isChecked() else self.tr("보기", "Show")
        self.history_toggle.setText(self.tr(f"내 기록    {state}", f"My history    {state}"))

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

    def refresh(self) -> None:
        current = _now()
        tracker = self.controller.tracker
        record = tracker.today(current)
        group = self.controller.online.get("group")
        raw_members = self.controller.online.get("members")
        my_id = (self.controller.online.get("auth") or {}).get("user_id")

        if group:
            self.room_name.setText(str(group.get("name") or self.tr("스터디방", "Study room")))
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
            self.room_presence.setText(self.tr("아직 참여한 방이 없습니다", "Not in a room yet"))
            members_known = True
            members = []

        self.manage.setText(self.tr("관리", "Manage"))
        self.manage.setAccessibleName(self.tr("스터디방 관리", "Manage study room"))

        self.own_title.setText(self.tr("나 · 오늘", "You · today"))
        own_status = self.status_text(
            "online" if tracker.status == "stopped" else tracker.status
        )
        self.own_status.setText(own_status)
        self.update_status_dot(
            self.own_dot, "online" if tracker.status == "stopped" else tracker.status
        )

        seconds = max(0, int(record.get("seconds") or 0))
        time_value = self.format_clock(seconds)
        time_goal = max(0, int(tracker.time_goal_minutes or 0))
        answer_value = max(0, int(record.get("answers") or 0))
        answer_goal = max(0, int(tracker.card_goal or 0))
        self.own_time.setText(
            f"{time_value} / {self.tr(f'{time_goal}분', f'{time_goal}m')}"
            if time_goal
            else time_value
        )
        self.own_answers.setText(
            f"{answer_value} / {answer_goal}" if answer_goal else str(answer_value)
        )
        self.time_caption.setText(self.tr("시간 / 목표", "Time / goal"))
        self.answer_caption.setText(self.tr("답변 / 목표", "Answers / goal"))

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

        error = self.controller.online.get("last_error")
        self.error_box.setVisible(bool(error))
        if error:
            self.error_text.setText(self.tr("동기화 지연", "Sync delayed"))
            self.error_text.setToolTip(str(error))
            self.retry.setText(self.tr("재시도", "Retry"))
