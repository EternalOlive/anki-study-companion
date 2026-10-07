"""Mini study summary strip shown at the bottom status bar when collapsed.

Mirrors the speaking-matrix solve study chip (cards.html):
- Green online dot (pulses/highlights when >1 online)
- Summary text: Online count, my count, and #1 leader (or studying alone)
- Miniature 144-slot color timeline strip showing slot leaders
- Clicking the badge pokes room members (prioritizing the #1 leader)
- Visible by default, can be toggled off in settings
"""

from __future__ import annotations

import math
import time
from datetime import datetime
from typing import Any

from aqt.qt import (
    QColor,
    QCursor,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPainter,
    QPalette,
    QRectF,
    Qt,
    QWidget,
)
try:
    from aqt.utils import tooltip
except Exception:
    tooltip = lambda *args, **kwargs: None

from .i18n import tr
from .pokes import POKE_COOLDOWN_SECONDS
from .room_activity import (
    SLOTS_PER_DAY,
    TIE_COLOR,
    member_colors,
    member_status,
    slot_leader,
    slot_rankings,
)
from .study_day import (
    DEFAULT_TIME_ZONE,
    room_time_zone,
    study_day,
    ten_minute_slot,
)
from .tracker import TIMEZONE


def _now() -> datetime:
    return datetime.now(TIMEZONE)


class MiniStripWidget(QWidget):
    """Compact 144-slot timeline strip showing slot leaders."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(52, 10)
        self.rankings: dict[int, list[tuple[str, int]]] = {}
        self.colors: dict[str, str] = {}
        self.current_slot: int | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def update_strip(
        self,
        rankings: dict[int, list[tuple[str, int]]],
        colors: dict[str, str],
        current_slot: int | None = None,
    ) -> None:
        self.rankings = dict(rankings)
        self.colors = dict(colors)
        self.current_slot = current_slot
        self.update()

    def paintEvent(self, _event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w = max(1, self.width())
        h = max(1, self.height())

        # Background track
        track = self.palette().color(QPalette.ColorRole.WindowText)
        track.setAlpha(26)
        painter.fillRect(0, 0, w, h, track)

        # Draw slot leader bars
        if self.rankings:
            for slot, ranking in self.rankings.items():
                if not ranking or not (0 <= slot < SLOTS_PER_DAY):
                    continue
                x1 = round(slot * w / SLOTS_PER_DAY)
                x2 = max(x1 + 1, round((slot + 1) * w / SLOTS_PER_DAY))
                leader = slot_leader(ranking)
                color_hex = self.colors.get(leader, TIE_COLOR) if leader else TIE_COLOR
                painter.fillRect(x1, 0, x2 - x1, h, QColor(color_hex))

        # Draw now marker
        if self.current_slot is not None and 0 <= self.current_slot < SLOTS_PER_DAY:
            now_x = min(w - 1, max(0, round(self.current_slot * w / SLOTS_PER_DAY)))
            painter.fillRect(now_x, 0, 1, h, QColor("#FF3B30"))


class CollapsedStudyStrip(QFrame):
    """Clickable bottom study badge shown in Anki status bar when panel is collapsed."""

    def __init__(self, controller: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.controller = controller
        self.lead_uid: str | None = None
        self._hovered = False
        self._poke_cooldowns: dict[str, float] = {}

        self.setObjectName("collapsedStudyStrip")
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(7, 2, 7, 2)
        layout.setSpacing(6)

        # Green online dot
        self.dot_label = QLabel(self)
        self.dot_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._update_dot_style(pulse=False)
        layout.addWidget(self.dot_label)

        # Info text (Online N · Me X · #1 Leader Y)
        self.info_label = QLabel(self)
        self.info_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        font = self.info_label.font()
        font.setPointSize(max(9, font.pointSize() - 1))
        self.info_label.setFont(font)
        layout.addWidget(self.info_label)

        # Separator bullet
        self.sep_label = QLabel("·", self)
        self.sep_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        muted = self.palette().color(QPalette.ColorRole.PlaceholderText).name()
        self.sep_label.setStyleSheet(f"color: {muted}; font-weight: bold;")
        layout.addWidget(self.sep_label)

        # Mini timeline strip
        self.strip_widget = MiniStripWidget(self)
        layout.addWidget(self.strip_widget)

    def _update_dot_style(self, pulse: bool) -> None:
        if pulse:
            self.dot_label.setStyleSheet(
                "background-color: #34C759; border-radius: 4px; min-width: 8px; max-width: 8px; "
                "min-height: 8px; max-height: 8px; border: 1.5px solid rgba(52, 199, 89, 0.45);"
            )
        else:
            self.dot_label.setStyleSheet(
                "background-color: #34C759; border-radius: 3px; min-width: 6px; max-width: 6px; "
                "min-height: 6px; max-height: 6px;"
            )

    def enterEvent(self, event: Any) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = min(rect.height() / 2.0, 10.0)

        # Pill background
        bg = self.palette().color(QPalette.ColorRole.Button)
        bg.setAlpha(190 if self._hovered else 110)
        border = self.palette().color(
            QPalette.ColorRole.Highlight if self._hovered else QPalette.ColorRole.Mid
        )
        border.setAlpha(200 if self._hovered else 120)

        painter.setPen(border)
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, radius, radius)
        super().paintEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.trigger_poke()
        super().mouseReleaseEvent(event)

    def _get_cooldowns(self) -> dict[str, float]:
        panel_body = getattr(self.controller, "panel_body", None)
        if panel_body and hasattr(panel_body, "poke_cooldowns") and isinstance(panel_body.poke_cooldowns, dict):
            return panel_body.poke_cooldowns
        return self._poke_cooldowns

    def trigger_poke(self) -> None:
        """Poke a room member on click, following speaking-matrix logic."""
        group = (getattr(self.controller, "online", None) or {}).get("group")
        loc = getattr(self.controller, "locale", "ko")
        if not group:
            return

        pokes_avail = getattr(self.controller, "pokes_available", None)
        if not callable(pokes_avail) or not pokes_avail():
            tooltip(tr("아직 찌르기 기능이 지원되지 않는 방이에요", "Poke is not supported yet", locale=loc), period=2500)
            return

        auth = (getattr(self.controller, "online", None) or {}).get("auth") or {}
        my_id = str(auth.get("user_id") or "")
        members = (getattr(self.controller, "online", None) or {}).get("members") or []
        other_members = [
            m for m in members
            if isinstance(m, dict) and str(m.get("user_id") or "") != my_id
        ]
        if not other_members:
            tooltip(tr("찌를 친구가 없어요", "No friends to poke", locale=loc), period=2500)
            return

        current_dt = _now()
        online_others = [m for m in other_members if member_status(m, current_dt) != "offline"]
        candidates = online_others if online_others else list(other_members)
        non_dnd = [m for m in candidates if not m.get("dnd")]
        if not non_dnd:
            tooltip(tr("모든 친구가 방해 금지 모드 중이에요", "All friends are in Do Not Disturb mode", locale=loc), period=2500)
            return
        candidates = non_dnd

        cooldowns = self._get_cooldowns()
        now_mono = time.monotonic()
        ready = [
            m for m in candidates
            if cooldowns.get(str(m.get("user_id") or ""), 0.0) <= now_mono
        ]

        if ready:
            lead_target = next(
                (m for m in ready if str(m.get("user_id") or "") == self.lead_uid),
                None,
            )
            target = lead_target or ready[0]
        else:
            candidates.sort(key=lambda m: cooldowns.get(str(m.get("user_id") or ""), 0.0))
            target = candidates[0]

        target_id = str(target.get("user_id") or "")
        if cooldowns.get(target_id, 0.0) > now_mono:
            rem_sec = max(1, math.ceil(cooldowns.get(target_id, 0.0) - now_mono))
            tooltip(
                tr(f"모두 찔렀어요 ({rem_sec}초 뒤 가능)", f"All poked (try again in {rem_sec}s)", locale=loc),
                period=2500,
            )
            return

        panel_body = getattr(self.controller, "panel_body", None)
        if panel_body and hasattr(panel_body, "poke_member"):
            panel_body.poke_member(target)
        else:
            poke_fn = getattr(self.controller, "poke_member", None)
            if callable(poke_fn):
                poke_fn(target)
                self._poke_cooldowns[target_id] = now_mono + POKE_COOLDOWN_SECONDS
        self.update_state()

    def update_state(self) -> None:
        """Update badge texts, timeline strip, and tooltip from current room state."""
        group = (getattr(self.controller, "online", None) or {}).get("group")
        if not group:
            self.hide()
            return

        loc = getattr(self.controller, "locale", "ko")
        auth = (getattr(self.controller, "online", None) or {}).get("auth") or {}
        my_id = str(auth.get("user_id") or "")
        raw_members = (getattr(self.controller, "online", None) or {}).get("members") or []
        current = _now()

        tz = room_time_zone(group) if callable(room_time_zone) else getattr(getattr(self.controller, "tracker", None), "time_zone", DEFAULT_TIME_ZONE)
        today = study_day(current, tz).isoformat()
        cur_slot = ten_minute_slot(current, tz)

        todays = [
            m for m in raw_members
            if isinstance(m, dict) and m.get("user_id") and m.get("study_day") in (None, today)
        ]

        other_members = [m for m in todays if str(m.get("user_id") or "") != my_id]
        online_others = [m for m in other_members if member_status(m, current) != "offline"]
        n_study = len(online_others) + 1

        rankings = slot_rankings(todays)
        keys = sorted(rankings.keys())

        ids = [str(m.get("user_id")) for m in todays if m.get("user_id")]
        if my_id and my_id not in ids:
            ids.append(my_id)
        my_col_fn = getattr(getattr(self.controller, "panel_body", None), "my_color", None)
        my_col_str = my_col_fn() if callable(my_col_fn) else self.palette().color(QPalette.ColorRole.Highlight).name()
        colors = member_colors(ids, my_id if my_id else None, my_col_str)

        lead_uid = None
        lead_text = ""

        if n_study == 1:
            # Solo: total answers today
            my_record = getattr(self.controller, "study_record", lambda _dt: {})(current) if hasattr(self.controller, "study_record") else {}
            live_record = self.controller.tracker.today(current) if hasattr(getattr(self.controller, "tracker", None), "today") else {}
            my_today_answers = max(
                int(my_record.get("answers") or 0),
                int(live_record.get("answers") or 0),
            )
            my_cnt_text = tr(f"나 {my_today_answers}개", f"Me {my_today_answers}", locale=loc)
            lead_text = tr("혼자 공부 중", "Studying alone", locale=loc)
        else:
            # 2+ members: answers in current 10-minute slot
            my_slot_answers = 0
            my_member = next((m for m in todays if str(m.get("user_id") or "") == my_id), None)
            if my_member:
                for b in my_member.get("activity_buckets") or []:
                    if b.get("slot") == cur_slot:
                        my_slot_answers = max(0, int(b.get("answer_count") or 0))
                        break
            my_cnt_text = tr(f"나 {my_slot_answers}개", f"Me {my_slot_answers}", locale=loc)

            if keys:
                latest_sl = max(keys)
                arr = rankings[latest_sl]
                if arr:
                    is_tie = len(arr) > 1 and arr[0][1] == arr[1][1]
                    if is_tie:
                        lead_text = tr(f"1위 공동 {arr[0][1]}개", f"#1 Tied {arr[0][1]}", locale=loc)
                    else:
                        top_uid, top_count = arr[0]
                        lead_uid = top_uid
                        if top_uid == my_id:
                            lead_text = tr(f"1위 나 {top_count}개", f"#1 Me {top_count}", locale=loc)
                        else:
                            top_m = next((m for m in todays if str(m.get("user_id") or "") == top_uid), None)
                            display_fn = getattr(self.controller, "display_member_name", None)
                            name = (
                                display_fn(top_m)
                                if callable(display_fn) and top_m
                                else (top_m.get("display_name") if top_m else tr("친구", "Friend", locale=loc))
                            )
                            lead_text = tr(f"1위 {name} {top_count}개", f"#1 {name} {top_count}", locale=loc)

        self.lead_uid = lead_uid

        # Tooltip
        poke_target_id = lead_uid if (lead_uid and lead_uid != my_id) else (
            str(online_others[0].get("user_id")) if online_others else (
                str(other_members[0].get("user_id")) if other_members else None
            )
        )
        if poke_target_id:
            self.setToolTip(tr("클릭하면 찌르기", "Click to poke", locale=loc))
        else:
            self.setToolTip(tr("스터디 참여 중", "In study room", locale=loc))

        self._update_dot_style(pulse=n_study > 1)

        online_str = tr(f"온라인 {n_study}명", f"Online {n_study}", locale=loc)
        text_parts = [online_str, my_cnt_text]
        if lead_text:
            text_parts.append(f"<b>{lead_text}</b>")
        self.info_label.setText(" · ".join(text_parts))

        self.strip_widget.update_strip(rankings, colors, cur_slot)
        self.strip_widget.setVisible(bool(keys))
        self.sep_label.setVisible(bool(keys))
