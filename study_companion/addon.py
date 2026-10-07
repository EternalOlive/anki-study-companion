"""PC Anki integration for the local study tracker."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aqt import gui_hooks, mw
from anki import lang
from aqt.qt import (
    QApplication,
    QDockWidget,
    QEvent,
    QLabel,
    QHBoxLayout,
    QScrollArea,
    QWidget,
    QObject,
    QTimer,
    Qt,
)
from aqt.utils import showWarning, tooltip

from .online import (
    DeviceSyncLedger,
    DeviceSnapshotConflict,
    PokeUnavailable,
    SupabaseClient,
    SupabaseError,
    load_or_create_device_id,
    profile_device_id,
)
from .nicknames import (
    canonical_nickname,
    disambiguate_nickname,
    localize_nickname,
    sanitize_display_name,
    validate_display_name,
)
from .outbox import SyncOutbox
from .pokes import POKE_UNAVAILABLE_RETRY_SECONDS, poke_message
from .record_status import (
    LOCAL_READ,
    LOCAL_SAVE,
    RecordStatusLedger,
    merge_pending_summaries,
)
from .ux_services import (
    ANSWER_GOAL_MAX,
    TIME_GOAL_MAX_MINUTES,
    build_invite_message,
    validate_goal,
)
from .activity import weekly_activity
from .realtime import (
    RealtimeClient,
    apply_presence_to_members,
    apply_review_tick_to_members,
)
from .confetti import trigger_confetti
from .i18n import SUPPORTED_LOCALES
from .room_activity import (
    member_status,
    presence_status,
    ranked_places,
    shareable_deck_name,
    slot_rankings,
)
from .reviews import ReviewHistory
from .study_day import (
    DEFAULT_TIME_ZONE,
    current_day_bounds,
    day_bounds,
    room_time_zone,
    study_day,
    ten_minute_slot,
)
from .collapsed_strip import CollapsedStudyStrip
from .panel import PanelToggleButton, StudyPanel
from .tracker import (
    StudyTracker,
    TIMEZONE,
)


def now() -> datetime:
    return datetime.now(TIMEZONE)


class ConfigReadError(OSError):
    """The existing profile state could not be trusted and must not be replaced."""


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _atomic_write_text(path: Path, payload: str) -> None:
    _atomic_write_bytes(path, payload.encode("utf-8"))


def _load_config(path: Path) -> tuple[dict, ConfigReadError | None]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}, None
    except OSError as error:
        return {}, ConfigReadError(f"설정 파일을 읽지 못했습니다: {error}")
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("top-level JSON value must be an object")
        for key in (
            "online", "records", "deck_records", "review_history",
            "legacy_tracker_records", "ui_state", "record_namespaces",
        ):
            if key in data and not isinstance(data[key], dict):
                raise ValueError(f"{key} must be an object")
        return data, None
    except (UnicodeError, ValueError, TypeError) as error:
        backup = path.with_name(path.name + ".corrupt-backup")
        try:
            _atomic_write_bytes(backup, raw)
        except OSError:
            # The original remains authoritative and untouched even when a
            # diagnostic backup cannot be written.
            pass
        return {}, ConfigReadError(f"설정 파일이 손상되었습니다: {error}")


def _not_group_member_error(error) -> bool:
    try:
        from .sync import is_kicked_error
        return is_kicked_error(error)
    except Exception:
        return str(error).strip().casefold() == "not a member of this group"


def _continue_after_review_batch_error(error) -> bool:
    """Skip only deterministic batch errors; transport/server failures back off."""
    if "review day archived" in str(error).casefold():
        return True
    return getattr(error, "status", None) in (400, 409, 422)


class InputWatcher(QObject):
    def __init__(self, controller):
        super().__init__(mw)
        self.controller = controller

    def eventFilter(self, watched, event):
        if watched is mw and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self.controller._position_panel_expand()
        if mw.state == "review":
            if event.type() in (
                QEvent.Type.KeyPress,
                QEvent.Type.MouseButtonPress,
                QEvent.Type.Wheel,
            ) and mw.isActiveWindow():
                if event.type() == QEvent.Type.KeyPress and event.isAutoRepeat():
                    return False
                # Only interaction with the review webviews counts as study.
                review = getattr(mw, "reviewer", None)
                views = (getattr(mw, "web", None), getattr(review, "bottom", None))
                target = watched
                while target is not None:
                    if target in views:
                        self.controller.input()
                        break
                    target = target.parent()
            elif event.type() == QEvent.Type.ApplicationDeactivate:
                self.controller.pause()
        return False


class Controller:
    def __init__(self):
        self.path = Path(mw.pm.profileFolder()) / "study_companion.json"
        data, self.config_read_error = _load_config(self.path)
        self.online = data.get("online", {})
        initial_time_zone = room_time_zone(self.online.get("group"))
        self.study_day_scheme = f"room-04-v1|{initial_time_zone}"
        previous_scheme = data.get("study_day_scheme")
        self.legacy_tracker_records = data.get("legacy_tracker_records", {})
        tracker_records = data.get("records")
        tracker_deck_records = data.get("deck_records")
        if previous_scheme != self.study_day_scheme:
            if tracker_records or tracker_deck_records:
                legacy_key = str(previous_scheme or "calendar-midnight-v0")
                self.legacy_tracker_records.setdefault(
                    legacy_key,
                    {"records": tracker_records or {}, "deck_records": tracker_deck_records or {}},
                )
            tracker_records = {}
            tracker_deck_records = {}
        self.tracker = StudyTracker(
            records=tracker_records,
            deck_records=tracker_deck_records,
            time_goal_minutes=data.get("time_goal_minutes", 0),
            card_goal=data.get("card_goal", 0),
            time_zone=initial_time_zone,
            record_namespaces=data.get("record_namespaces"),
        )
        self.review_history = ReviewHistory(data.get("review_history", {}))
        self.review_history.migrate_study_days(initial_time_zone)
        self._weekly_activity = None
        self.review_dirty = True
        self.review_query_in_flight = False
        self.review_syncing = False
        self.review_allow_removals = False
        self.review_upload_requested = True
        device_path = self.path.parent.parent / "study_companion-device.json"
        try:
            installation_id = load_or_create_device_id(device_path)
        except OSError:
            # A read-only Anki data directory is unusual, but keeping a stable
            # fallback in this add-on's local state is safer than disabling sync.
            import uuid
            installation_id = self.online.setdefault(
                "installation_id", str(uuid.uuid4())
            )
        self.device_id = profile_device_id(installation_id, self.path.parent)
        self.device_ledger = DeviceSyncLedger(
            self.online.setdefault("device_sync", {})
        )
        self.device_ledger.bind_device(self.device_id)
        self.sync_outbox = SyncOutbox(self.online.setdefault("sync_outbox", {}))
        self.sync_outbox.bind_device(self.device_id)
        self.record_status = RecordStatusLedger(
            self.online.setdefault("record_status", {})
        )
        if self.config_read_error is not None:
            self.record_status.set_error(LOCAL_READ, self.config_read_error)
        self.locale = data.get("language", "ko" if lang.current_lang.startswith("ko") else "en")
        if self.locale not in SUPPORTED_LOCALES:
            self.locale = "ko"
        self.ui_state = data.get("ui_state", {})
        self.client = SupabaseClient()
        self.realtime = RealtimeClient(apikey=getattr(self.client, "key", ""), parent=mw)
        self.realtime.review_tick_received.connect(self._on_realtime_review_tick)
        self.realtime.presence_changed.connect(self._on_realtime_presence_changed)
        self.sync_in_flight = False
        self.sync_pending = False
        self.sync_failure_count = 0
        self.next_sync_attempt_at = 0.0
        self._member_cache_key = None
        self._last_member_fetch_at = 0
        self.identity_in_flight = False
        self.identity_generation = 0
        self._auth_refresh_lock = threading.Lock()
        self._auth_refresh_cache = {}
        self._auth_persistence_error = None
        self.closed = False
        self.label = QLabel(mw)
        mw.statusBar().addPermanentWidget(self.label)
        self.mini_study_badge = CollapsedStudyStrip(self, mw)
        mw.statusBar().addPermanentWidget(self.mini_study_badge)
        self.mini_study_badge.hide()
        self.action = mw.form.menuTools.addAction(self.t("스터디 관리", "Study settings"))
        self.action.triggered.connect(self.show_dialog)
        self._build_side_panel()
        self.watcher = InputWatcher(self)
        QApplication.instance().installEventFilter(self.watcher)
        self.timer = QTimer(mw)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.ticks_since_save = 0
        tz = getattr(self.tracker, "time_zone", DEFAULT_TIME_ZONE)
        self._last_ten_min_slot = ten_minute_slot(now(), tz)
        self._celebrated_slots: set[tuple[str, int]] = set()
        self.refresh()
        QTimer.singleShot(0, self.refresh_review_history)
        QTimer.singleShot(1000, self.ensure_online_identity)

    def close(self):
        self.closed = True
        if getattr(self, "realtime", None) is not None:
            self.realtime.leave_room()
        settings_dialog = getattr(self, "_settings_dialog", None)
        if settings_dialog is not None:
            settings_dialog.reject()
        self._cancel_identity_bootstrap()
        self.tracker.leave_review(now())
        try:
            self.save()
        except ConfigReadError:
            pass
        self.timer.stop()
        QApplication.instance().removeEventFilter(self.watcher)
        mw.statusBar().removeWidget(self.label)
        if hasattr(self, "mini_study_badge"):
            mw.statusBar().removeWidget(self.mini_study_badge)
            self.mini_study_badge.deleteLater()
        self.panel_expand.deleteLater()
        mw.form.menuTools.removeAction(self.action)
        mw.form.menuTools.removeAction(self.panel_action)
        mw.removeDockWidget(self.panel)
        self.panel.deleteLater()

    def save(self):
        config_read_error = getattr(self, "config_read_error", None)
        if config_read_error is not None:
            self.record_status.set_error(LOCAL_READ, config_read_error)
            self.record_status.set_error(LOCAL_SAVE, config_read_error)
            raise config_read_error
        previous_save = self.record_status.success_at(LOCAL_SAVE)
        self.record_status.mark_success(LOCAL_SAVE)
        try:
            data = self.tracker.snapshot()
            data["online"] = self.online
            data["language"] = self.locale
            data["ui_state"] = self.ui_state
            data["review_history"] = self.review_history.state
            data["study_day_scheme"] = self.study_day_scheme
            data["legacy_tracker_records"] = self.legacy_tracker_records
            payload = json.dumps(data, ensure_ascii=False)
            _atomic_write_text(self.path, payload)
        except Exception as error:
            self.record_status.restore_success(LOCAL_SAVE, previous_save)
            self.record_status.set_error(LOCAL_SAVE, error)
            raise
        self.record_status.clear_error(LOCAL_SAVE)
        self._auth_persistence_error = None

    def update_daily_goals(self, *, time_goal_minutes=None, card_goal=None):
        """Persist a partial goal edit through the same offline-safe sync path."""
        return self.update_local_settings(
            time_goal_minutes=time_goal_minutes, card_goal=card_goal
        )

    def update_local_settings(
        self, *, time_goal_minutes=None, card_goal=None, locale=None,
        share_deck_name=None, show_collapsed_strip=None, do_not_disturb=None,
        celebrate_confetti=None,
    ):
        """Commit only explicitly edited fields, including settings-dialog drafts."""
        updates = {}
        if time_goal_minutes is not None:
            updates["time_goal_minutes"] = validate_goal(
                time_goal_minutes, maximum=TIME_GOAL_MAX_MINUTES
            )
        if card_goal is not None:
            updates["card_goal"] = validate_goal(
                card_goal, maximum=ANSWER_GOAL_MAX
            )
        previous = {
            "time_goal_minutes": self.tracker.time_goal_minutes,
            "card_goal": self.tracker.card_goal,
        }
        previous_locale = self.locale
        had_share = "share_deck_name" in self.online
        previous_share = self.online.get("share_deck_name")
        ui_state = getattr(self, "ui_state", None)
        if ui_state is None:
            self.ui_state = ui_state = {}
        had_strip = "show_collapsed_strip" in ui_state
        previous_strip = ui_state.get("show_collapsed_strip", True)
        had_dnd = "do_not_disturb" in ui_state
        previous_dnd = ui_state.get("do_not_disturb", False)
        had_confetti = "celebrate_confetti" in ui_state
        previous_confetti = ui_state.get("celebrate_confetti", True)
        if locale is not None and locale not in SUPPORTED_LOCALES:
            raise ValueError("invalid language")
        if share_deck_name is not None and not isinstance(share_deck_name, bool):
            raise ValueError("invalid sharing preference")
        if show_collapsed_strip is not None and not isinstance(show_collapsed_strip, bool):
            raise ValueError("invalid show_collapsed_strip preference")
        if do_not_disturb is not None and not isinstance(do_not_disturb, bool):
            raise ValueError("invalid do_not_disturb preference")
        if celebrate_confetti is not None and not isinstance(celebrate_confetti, bool):
            raise ValueError("invalid celebrate_confetti preference")
        changed = any(getattr(self.tracker, key) != value for key, value in updates.items())
        changed |= locale is not None and locale != self.locale
        changed |= share_deck_name is not None and share_deck_name != bool(previous_share)
        changed |= show_collapsed_strip is not None and show_collapsed_strip != bool(previous_strip)
        changed |= do_not_disturb is not None and do_not_disturb != bool(previous_dnd)
        changed |= celebrate_confetti is not None and celebrate_confetti != bool(previous_confetti)
        if not changed:
            return False
        for key, value in updates.items():
            setattr(self.tracker, key, value)
        if locale is not None:
            self.locale = locale
        if share_deck_name is not None:
            self.online["share_deck_name"] = share_deck_name
        if show_collapsed_strip is not None:
            self.ui_state["show_collapsed_strip"] = show_collapsed_strip
        if do_not_disturb is not None:
            self.ui_state["do_not_disturb"] = do_not_disturb
        if celebrate_confetti is not None:
            self.ui_state["celebrate_confetti"] = celebrate_confetti
        try:
            self.save()
        except Exception:
            self.tracker.time_goal_minutes = previous["time_goal_minutes"]
            self.tracker.card_goal = previous["card_goal"]
            self.locale = previous_locale
            if had_share:
                self.online["share_deck_name"] = previous_share
            else:
                self.online.pop("share_deck_name", None)
            if had_strip:
                self.ui_state["show_collapsed_strip"] = previous_strip
            else:
                self.ui_state.pop("show_collapsed_strip", None)
            if had_dnd:
                self.ui_state["do_not_disturb"] = previous_dnd
            else:
                self.ui_state.pop("do_not_disturb", None)
            if had_confetti:
                self.ui_state["celebrate_confetti"] = previous_confetti
            else:
                self.ui_state.pop("celebrate_confetti", None)
            raise
        self.refresh()
        self.sync_async(force=True)
        return True

    def is_do_not_disturb(self) -> bool:
        ui_state = getattr(self, "ui_state", None)
        if not isinstance(ui_state, dict):
            return False
        return bool(ui_state.get("do_not_disturb", False))

    def toggle_do_not_disturb(self) -> bool:
        new_val = not self.is_do_not_disturb()
        self.update_local_settings(do_not_disturb=new_val)
        msg = (
            self.t("방해 금지 모드를 켰습니다. (찌르기 알림 끔)", "Do Not Disturb turned on.")
            if new_val
            else self.t("방해 금지 모드를 껐습니다.", "Do Not Disturb turned off.")
        )
        tooltip(msg, period=2000)
        return new_val

    def invite_message(self, setup_url=None):
        return build_invite_message(
            dict(self.online.get("group") or {}), self.locale, setup_url
        )

    def record_status_snapshot(self):
        auth = self.online.get("auth") or {}
        group = self.online.get("group") or {}
        user_id = auth.get("user_id")
        group_id = group.get("id")
        pending = None
        if user_id and group_id:
            clock_now = time.time()
            recent_day = (
                study_day(now(), self.tracker.time_zone) - timedelta(days=1)
            ).isoformat()
            pending = merge_pending_summaries(
                self.sync_outbox.pending_summary(
                    user_id=user_id, group_id=group_id, device_id=self.device_id,
                    now=clock_now,
                ),
                self.review_history.pending_summary(
                    user_id, group_id, since_day=recent_day, now=clock_now
                ),
            )
        return self.record_status.snapshot(
            user_id=user_id, group_id=group_id, pending=pending
        )

    def _on_realtime_review_tick(self, user_id: str, slot: int, answers: int, time_ms: int):
        members = self.online.get("members")
        if isinstance(members, list):
            member_ids = {str(m.get("user_id")) for m in members if isinstance(m, dict)}
            if user_id in member_ids:
                if apply_review_tick_to_members(members, user_id, slot, answers, time_ms):
                    self.refresh()

    def _on_realtime_presence_changed(self, presences: dict):
        members = self.online.get("members")
        if isinstance(members, list):
            current_iso = now().isoformat()
            if apply_presence_to_members(members, presences, current_iso):
                self.refresh()

    def _current_presence_meta(self, current=None):
        current = current or now()
        time_zone = getattr(self.tracker, "time_zone", DEFAULT_TIME_ZONE)
        status = presence_status(
            getattr(self.tracker, "status", "offline"),
            getattr(self.tracker, "last_input_at", None),
            current,
        )
        deck_name = shareable_deck_name(
            bool(self.online.get("share_deck_name", True)),
            getattr(self.tracker, "current_deck_name", None),
            getattr(self.tracker, "current_deck_day", None),
            study_day(current, time_zone).isoformat(),
        )
        return status, deck_name

    def _sync_realtime_connection(self):
        realtime = getattr(self, "realtime", None)
        if realtime is None or not realtime.is_available():
            return
        group = self.online.get("group") or {}
        auth = self.online.get("auth") or {}
        group_id = group.get("id")
        user_id = auth.get("user_id")
        token = self._access_token()
        if group_id and user_id:
            realtime.join_room(group_id, user_id, token=token)
            status, deck_name = self._current_presence_meta()
            display_name = sanitize_display_name(
                self.online.get("display_name"),
                user_id,
            )
            realtime.update_presence(
                status=status,
                current_deck_name=deck_name,
                display_name=display_name,
                dnd=self.is_do_not_disturb(),
            )
        else:
            realtime.leave_room()

    def tick(self):
        self.tracker.tick(now())
        current = now()
        if self.review_dirty or self.review_history.today(
            study_day(current, self.tracker.time_zone).isoformat()
        ) is None:
            self.refresh_review_history()
        self.ticks_since_save += 1
        sync_interval = 30
        if self.ticks_since_save >= sync_interval:
            self.save()
            self.sync_async()
            self.ticks_since_save = 0
        self._sync_realtime_connection()
        time_zone = getattr(self.tracker, "time_zone", DEFAULT_TIME_ZONE)
        curr_slot = ten_minute_slot(current, time_zone)
        if curr_slot != getattr(self, "_last_ten_min_slot", curr_slot):
            prev_slot = (curr_slot - 1) % 144
            self._last_ten_min_slot = curr_slot
            self._check_and_celebrate_slot(prev_slot, current)
        self.refresh()

    def input(self):
        self.tracker.input(now())
        self.refresh()

    def pause(self):
        self.tracker.pause(now())
        self.save()
        self.refresh()

    def show_question(self, card):
        current = now()
        deck_id = str(card.odid or card.did)
        try:
            deck_name = mw.col.decks.name(int(deck_id))
        except Exception:
            deck_name = self.t("현재 덱", "Current deck")
        self.tracker.set_deck(deck_id, deck_name, current)
        self.tracker.enter_review(current)
        self.refresh()

    def answer(self, reviewer, card, ease):
        current = now()
        self.tracker.answer(current)
        self.review_dirty = True
        self.refresh_review_history()
        self.save()
        if getattr(self, "realtime", None) and self.realtime.is_connected():
            slot = ten_minute_slot(current, getattr(self.tracker, "time_zone", "Asia/Seoul"))
            self.realtime.broadcast_review_tick(slot=slot, count=1, time_ms=0)
        self.refresh()

    def state_changed(self, new_state, old_state):
        if old_state == "review" and new_state != "review":
            self.tracker.leave_review(now())
            self.save()
            self.refresh()
            self.sync_async(force=True)

    def refresh(self):
        current = now()
        review_record = self.study_record(current)
        live_record = self.tracker.today(current)
        duration = self.panel_body.format_clock(int(live_record["seconds"]))
        answers = max(
            int(review_record.get("answers") or 0),
            int(live_record.get("answers") or 0),
        )
        status = {"studying": self.t("공부 중", "Studying"), "paused": self.t("잠시 멈춤", "Paused"), "stopped": self.t("접속 중", "Online")}[
            presence_status(self.tracker.status, self.tracker.last_input_at, current)
        ]
        time_goal = f"/{self.panel_body.format_clock(self.tracker.time_goal_minutes * 60)}" if self.tracker.time_goal_minutes else ""
        card_goal = f"/{self.tracker.card_goal}" if self.tracker.card_goal else ""
        self.label.setText(
            f"{self.t('오늘', 'Today')} {duration}{time_goal}  "
            f"{self.t('답변', 'Answers')} {answers}{card_goal}  {status}"
        )
        self.refresh_panel()
        self.refresh_collapsed_strip()

    def study_record(self, current):
        day = study_day(current, self.tracker.time_zone).isoformat()
        return self.review_history.today(day) or {"seconds": 0, "answers": 0}

    def weekly_record(self, current):
        from .study_day import DEFAULT_TIME_ZONE, study_day

        snapshot = self._weekly_activity
        if not isinstance(snapshot, dict):
            return None
        time_zone = getattr(getattr(self, "tracker", None), "time_zone", DEFAULT_TIME_ZONE)
        if snapshot.get("day") != study_day(current, time_zone).isoformat():
            return None
        current_collection = getattr(mw, "col", None)
        if current_collection is None:
            return None
        if snapshot.get("collection_identity") != id(current_collection):
            return None
        if snapshot.get("collection_key") != self.review_history.state.get("active_collection"):
            return None
        return snapshot.get("record")

    def refresh_review_history(self):
        """Read native logs through Anki's serialized collection queue, not the UI timer.

        Sync/restore can temporarily remove local rows, so absence alone is never
        sent as a deletion of another device's study history.
        """
        if self.closed or self.review_query_in_flight or self.review_syncing or not mw.col:
            return
        from aqt.operations import QueryOp
        from datetime import timezone as datetime_timezone
        from .study_day import (
            DEFAULT_TIME_ZONE,
            current_day_bounds,
            day_bounds,
            study_day,
        )

        current = now()
        time_zone = getattr(getattr(self, "tracker", None), "time_zone", DEFAULT_TIME_ZONE)
        today_date, _today_start, end = current_day_bounds(current, time_zone)
        today = today_date.isoformat()
        yesterday_date = today_date - timedelta(days=1)
        yesterday = yesterday_date.isoformat()
        start, _ = day_bounds(today_date - timedelta(days=13), time_zone)
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        self.review_dirty = False
        self.review_query_in_flight = True
        allow_removals = self.review_allow_removals
        self.review_allow_removals = False
        upload_requested = self.review_upload_requested
        self.review_upload_requested = False

        def query(col):
            rows = col.db.all(
                "select id, cid, time, ease, type from revlog where id >= ? and id < ? order by id",
                start_ms, end_ms,
            )
            aggregate_current = now()
            if study_day(aggregate_current, time_zone) != today_date:
                # Keep the snapshot on the queried day. The next timer tick
                # immediately issues the new day's correctly bounded query.
                aggregate_current = current
            return (
                str(col.crt), id(col), rows,
                weekly_activity(rows, aggregate_current, time_zone),
            )

        def success(result):
            self.review_query_in_flight = False
            if self.closed:
                return
            if getattr(getattr(self, "tracker", None), "time_zone", DEFAULT_TIME_ZONE) != time_zone:
                # A room switch changed the calendar while this queued Anki
                # read was running. Discard its old-boundary snapshot and let
                # the next tick issue a correctly bounded query.
                self.review_dirty = True
                self.review_allow_removals |= allow_removals
                self.review_upload_requested |= upload_requested
                return
            collection_key, collection_identity, rows, weekly = result
            if mw.col is None or id(mw.col) != collection_identity:
                self.review_dirty = True
                self.review_allow_removals |= allow_removals
                self.review_upload_requested |= upload_requested
                return
            rows_by_day = {yesterday: [], today: []}
            for row in rows:
                try:
                    row_day = study_day(
                        datetime.fromtimestamp(int(row[0]) / 1000, datetime_timezone.utc),
                        time_zone,
                    ).isoformat()
                except (IndexError, TypeError, ValueError, OverflowError, OSError):
                    continue
                if row_day in rows_by_day:
                    rows_by_day[row_day].append(row)
            self.review_history.observe(collection_key, yesterday, rows_by_day[yesterday])
            self.review_history.observe(
                collection_key, today, rows_by_day[today], allow_removals=allow_removals
            )
            auth = self.online.get("auth") or {}
            group = self.online.get("group") or {}
            if auth.get("user_id") and group.get("id"):
                self.review_history.mark_route_days(
                    auth["user_id"],
                    group["id"],
                    collection_key,
                    [yesterday, today],
                    since_day=yesterday,
                )
            self.review_history.compact(
                (today_date - timedelta(days=2)).isoformat(),
                discard_unrouted=not bool(group),
            )
            self._weekly_activity = {
                "day": today,
                "collection_key": str(collection_key),
                "collection_identity": collection_identity,
                "record": weekly,
            }
            self.online.pop("review_error", None)
            status_ledger = getattr(self, "record_status", None)
            if status_ledger is not None:
                status_ledger.mark_success(LOCAL_READ)
            try:
                self.save()
            except OSError:
                self.review_dirty = True
                self.review_allow_removals |= allow_removals
                self.review_upload_requested |= upload_requested
                self.online["review_error"] = self.t("학습 기록 저장 실패", "Could not save review history")
                return
            self.refresh()
            if upload_requested:
                self.sync_async(force=True)

        def failure(error):
            self.review_query_in_flight = False
            if not self.closed:
                self._weekly_activity = None
                self.review_dirty = True
                self.review_allow_removals |= allow_removals
                self.review_upload_requested |= upload_requested
            self.online["review_error"] = self.t("Anki 학습 기록을 읽지 못했습니다", "Could not read Anki review history")
            status_ledger = getattr(self, "record_status", None)
            if status_ledger is not None:
                status_ledger.set_error(LOCAL_READ, error)

        try:
            QueryOp(parent=mw, op=query, success=success).failure(failure).run_in_background()
        except Exception as error:
            failure(error)

    def apply_room_time_zone(self, time_zone, current=None):
        """Rotate live counters and re-key cached reviews when rooms change zone."""
        target = str(time_zone or DEFAULT_TIME_ZONE)
        if target == self.tracker.time_zone:
            return False
        current = current or now()
        self.tracker.tick(current)
        self.tracker.set_time_zone(target)
        self.study_day_scheme = f"room-04-v1|{target}"
        self.review_history.migrate_study_days(target)
        self._weekly_activity = None
        self.review_dirty = True
        self.review_upload_requested = True
        return True

    def t(self, korean, english, ja="", zh_cn=""):
        try:
            from . import i18n
            return i18n.tr(korean, english, ja, zh_cn, locale=self.locale)
        except Exception:
            return english if self.locale == "en" else korean

    def set_locale(self, locale):
        self.locale = locale if locale in ("ko", "en", "ja", "zh_CN") else "ko"
        self.save()
        self.refresh()

    def display_member_name(self, member):
        name = sanitize_display_name(
            member.get("display_name"),
            member.get("user_id") or "local",
        )
        display = localize_nickname(name, self.locale)
        duplicates = {
            item.get("user_id") for item in self.online.get("members", [])
            if sanitize_display_name(item.get("display_name"), item.get("user_id") or "local") == name and item.get("user_id")
        }
        if len(duplicates) > 1:
            display = disambiguate_nickname(display, member["user_id"], True)
        return display

    def set_display_name(self, name: str) -> bool:
        """Update the user's custom nickname, save config, update profile on server, and notify room."""
        if not validate_display_name(name):
            return False
        self.online["display_name"] = name
        self.online["synced_display_name"] = ""
        self.save()

        auth = self.online.get("auth") or {}
        token = self._access_token()
        user_id = auth.get("user_id")
        if token and user_id:
            def _upload():
                try:
                    self.client.upsert_profile(token, user_id, name)
                    self.online["synced_display_name"] = name
                    self.save()
                except Exception:
                    pass
            threading.Thread(target=_upload, daemon=True).start()

            realtime = getattr(self, "realtime", None)
            if realtime is not None and realtime.is_available():
                status, deck_name = self._current_presence_meta()
                realtime.update_presence(
                    status=status,
                    current_deck_name=deck_name,
                    display_name=name,
                    dnd=self.is_do_not_disturb(),
                )
        self.refresh()
        return True

    def reset_display_name(self) -> None:
        """Reset the user's custom nickname back to default anonymous guest code."""
        auth = self.online.get("auth") or {}
        user_id = auth.get("user_id")
        if not user_id:
            self.online.pop("display_name", None)
            self.online.pop("synced_display_name", None)
            self.save()
            self.refresh()
            return

        default_name = canonical_nickname(user_id)
        self.online["display_name"] = default_name
        self.online["synced_display_name"] = ""
        self.save()

        token = self._access_token()
        if token:
            def _upload():
                try:
                    self.client.upsert_profile(token, user_id, default_name)
                    self.online["synced_display_name"] = default_name
                    self.save()
                except Exception:
                    pass
            threading.Thread(target=_upload, daemon=True).start()

            realtime = getattr(self, "realtime", None)
            if realtime is not None and realtime.is_available():
                status, deck_name = self._current_presence_meta()
                realtime.update_presence(
                    status=status,
                    current_deck_name=deck_name,
                    display_name=default_name,
                    dnd=self.is_do_not_disturb(),
                )
        self.refresh()

    def pokes_available(self):
        """False while the server is known to lack the poke RPCs."""
        until = float(getattr(self, "_pokes_unavailable_until", 0) or 0)
        return time.monotonic() >= until

    def _disable_pokes(self):
        self._pokes_unavailable_until = time.monotonic() + POKE_UNAVAILABLE_RETRY_SECONDS

    def poke_member(self, member):
        """Poke another member of the current room in the background.

        Returns False without a request when poking is not possible. A server
        without the poke migration hides the feature silently; other failures
        (rate limit, network) show a non-blocking tooltip.
        """
        group_id = (self.online.get("group") or {}).get("id")
        user_id = str((member or {}).get("user_id") or "")
        my_id = str((self.online.get("auth") or {}).get("user_id") or "")
        if not group_id or not user_id or user_id == my_id or not self.pokes_available():
            return False
        name = self.display_member_name(member)
        if member.get("dnd"):
            tooltip(self.t(f"{name} 님은 방해 금지 모드 중입니다.", f"{name} is in Do Not Disturb mode."), period=3000)
            return False
        unavailable = object()

        def operation(token):
            try:
                return self.client.poke_room_member(token, group_id, user_id)
            except PokeUnavailable:
                return unavailable

        def success(result):
            if result is unavailable:
                self._disable_pokes()
                self.refresh_panel()
                return
            tooltip(self.t(f"{name}님을 콕 찔렀어요", f"You poked {name}"), period=2500)

        self._run_authenticated_action(
            [], operation, success, self.t("찌르지 못했어요.", "Could not poke."),
            on_error=lambda message: tooltip(message, period=4000),
        )
        return True

    def show_pokes(self, pokes):
        """Show newly received pokes as one non-blocking tooltip."""
        if self.is_do_not_disturb():
            return
        members = {
            str(member.get("user_id")): member
            for member in (self.online.get("members") or [])
            if isinstance(member, dict) and member.get("user_id")
        }

        def name_for(user_id):
            return self.display_member_name(members.get(user_id) or {"user_id": user_id})

        message = poke_message(pokes, name_for, self.t)
        if message:
            tooltip(message, period=5000)

    def _check_and_celebrate_slot(self, slot: int, current: datetime):
        """Celebrate if current user ranked 1st (solo or tie) in the finished 10-min slot."""
        if self.is_do_not_disturb():
            return
        ui_state = getattr(self, "ui_state", None)
        if isinstance(ui_state, dict) and not ui_state.get("celebrate_confetti", True):
            return
        group = self.online.get("group")
        if not group:
            return
        time_zone = getattr(self.tracker, "time_zone", DEFAULT_TIME_ZONE)
        today_iso = study_day(current, time_zone).isoformat()
        slot_key = (today_iso, slot)
        if slot_key in self._celebrated_slots:
            return

        auth = self.online.get("auth") or {}
        my_id = str(auth.get("user_id") or "")
        if not my_id:
            return

        members = self.online.get("members") or []
        todays = [
            m for m in members
            if isinstance(m, dict) and m.get("user_id") and m.get("study_day") in (None, today_iso)
        ]
        if not any(m.get("activity_known") is True for m in todays):
            return

        rankings = slot_rankings(todays)
        ranking = rankings.get(slot, [])
        if not ranking:
            return

        places = ranked_places(ranking)
        first_place_users = [user_id for place, user_id, _ans in places if place == 1]
        if my_id in first_place_users:
            my_entry = next((item for item in places if item[1] == my_id), None)
            answers = my_entry[2] if my_entry else 0
            if answers > 0:
                self._celebrated_slots.add(slot_key)
                is_tie = len(first_place_users) > 1
                trigger_confetti(self, is_tie=is_tie, answers=answers)

    def test_confetti(self):
        """Trigger celebration confetti immediately for user testing."""
        trigger_confetti(
            self,
            is_tie=False,
            custom_message=self.t("축포 테스트! 1등 달성을 축하합니다!", "Confetti test! Congratulations on 1st place!"),
        )

    def _build_side_panel(self):
        self.panel = QDockWidget(self.t("스터디", "Study"), mw)
        self.panel.setObjectName("study_companion_panel")
        self.panel.setAllowedAreas(
            Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.panel.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable)
        self.panel_body = StudyPanel(self)
        title_bar = QWidget(self.panel)
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(0, 2, 4, 2)
        title_layout.addStretch()
        title_layout.addWidget(self.panel_body.collapse_panel)
        self.panel.setTitleBarWidget(title_bar)
        self.panel_scroll = QScrollArea(self.panel)
        self.panel_scroll.setWidgetResizable(True)
        self.panel_scroll.setMinimumWidth(0)
        self.panel_scroll.setWidget(self.panel_body)
        self.panel.setWidget(self.panel_scroll)
        mw.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.panel)
        self.panel_expand = PanelToggleButton(mw, expand=True)
        self.panel_expand.clicked.connect(
            lambda: self.set_panel_collapsed(self.panel.isVisible())
        )
        self.panel_expand.hide()
        self.panel_action = self.panel.toggleViewAction()
        self.panel_action.triggered.connect(lambda visible: self.set_panel_collapsed(not visible))
        mw.form.menuTools.addAction(self.panel_action)
        self.panel.show()
        QTimer.singleShot(
            0, lambda: self.set_panel_collapsed(
                bool(self.ui_state.get("panel_collapsed")), persist=False
            ) if not self.closed else None
        )

    def _position_panel_expand(self):
        button = getattr(self, "panel_expand", None)
        if button is None:
            return
        button.move(max(0, mw.width() - button.width() - 4), mw.menuBar().height() + 2)
        if button.isVisible():
            button.raise_()

    def set_panel_collapsed(self, collapsed, *, persist=True):
        collapsed = bool(collapsed)
        was_hidden = not self.panel.isVisible()
        if collapsed:
            current_width = self.panel.width()
            if self.panel.isVisible() and current_width >= 160:
                self.ui_state["panel_width"] = current_width
            self.panel.hide()
            self.panel_expand.show()
            self._position_panel_expand()
        else:
            self.panel_expand.hide()
            self.panel.setMaximumWidth(16777215)
            self.panel.setMinimumWidth(0)
            self.panel_body.set_collapsed(False)
            self.panel.setWindowTitle(self.t("스터디", "Study"))
            target_width = min(max(160, int(self.ui_state.get("panel_width") or 320)),
                               max(160, mw.width() // 2))
            self.panel.show()
            mw.resizeDocks([self.panel], [target_width], Qt.Orientation.Horizontal)
            self.panel_body.refresh()
        self.ui_state["panel_collapsed"] = collapsed
        self.panel_expand.expand = collapsed
        self.panel_expand.setToolTip(self.t(
            "스터디 패널 펼치기" if collapsed else "스터디 패널 접기",
            "Show study panel" if collapsed else "Hide study panel",
        ))
        self.panel_expand.setAccessibleName(self.panel_expand.toolTip())
        self.panel_expand.update()
        if persist:
            self.save()
        if not collapsed and was_hidden and persist:
            self.sync_async(force=True)
        self.refresh_collapsed_strip()

    def is_panel_collapsed(self) -> bool:
        if not hasattr(self, "panel"):
            return False
        return not self.panel.isVisible() or bool(self.ui_state.get("panel_collapsed"))

    def refresh_collapsed_strip(self):
        badge = getattr(self, "mini_study_badge", None)
        if badge is None:
            return
        in_room = bool(self.online.get("group"))
        collapsed = self.is_panel_collapsed()
        enabled = bool(self.ui_state.get("show_collapsed_strip", True))
        if in_room and collapsed and enabled:
            badge.update_state()
            badge.show()
        else:
            badge.hide()

    def refresh_panel(self):
        self.action.setText(self.t("스터디 관리", "Study settings"))
        self.panel.setWindowTitle(
            "" if self.panel_body.collapsed else self.t("스터디", "Study")
        )
        self.panel_action.setText(self.t("스터디 패널", "Study panel"))
        self.panel_expand.setToolTip(self.t(
            "스터디 패널 접기" if self.panel.isVisible() else "스터디 패널 펼치기",
            "Hide study panel" if self.panel.isVisible() else "Show study panel",
        ))
        self.panel_expand.setAccessibleName(self.panel_expand.toolTip())
        if self.panel.isVisible():
            self.panel_body.refresh()

    def show_dialog(self, checked=False, *, page=None):
        if self.closed:
            return
        existing = getattr(self, "_settings_dialog", None)
        if existing is not None:
            if page in ("create", "join", "record_status"):
                getattr(existing, "show_" + page)()
            existing.raise_()
            existing.activateWindow()
            return
        from .settings import SettingsDialog
        dialog = SettingsDialog(self, mw)
        if page in ("create", "join", "record_status"):
            getattr(dialog, "show_" + page)()
        self._settings_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._settings_dialog = None
            dialog.deleteLater()

    def ensure_online_identity(self):
        if getattr(self, "config_read_error", None) is not None:
            return
        if self._access_token():
            self.sync_async()
            return
        if self.identity_in_flight or self.online.get("email") or self.online.get("username"):
            return
        if self.online.get("guest_id"):
            self.online["last_error"] = (
                "기존 계정의 접속 정보가 만료되었습니다. 연결한 아이디 또는 이메일이 있다면 "
                "기존 계정 로그인으로 복구해 주세요."
            )
            self.save()
            return
        self.identity_in_flight = True
        self.identity_generation += 1
        generation = self.identity_generation

        def task():
            result = self.client.sign_in_anonymously()
            user = result.get("user") or {}
            user_id = user.get("id") or result.get("user_id")
            if not result.get("access_token") or not user_id:
                raise SupabaseError("고유번호 발급 응답이 올바르지 않습니다.")
            display_name = canonical_nickname(user_id)
            self.client.upsert_profile(result["access_token"], user_id, display_name)
            return result, display_name

        def done(future):
            if generation != self.identity_generation:
                return
            self.identity_in_flight = False
            try:
                result, display_name = future.result()
                if self._access_token():
                    return
                self._store_session(result, "", display_name)
                self.online["account_kind"] = "guest"
                self.online.pop("last_error", None)
                self.save()
            except Exception as error:
                self.online["last_error"] = str(error)
                self.save()

        mw.taskman.run_in_background(task, done)

    def _cancel_identity_bootstrap(self):
        self.identity_generation += 1
        self.identity_in_flight = False

    def restart_expired_guest(self):
        """Replace only an explicitly expired, unlinked guest identity.

        Review history, local goals, and account-scoped outbox entries remain
        intact.  Clearing the old room/user route prevents those entries from
        being uploaded under the newly issued guest account.
        """
        if getattr(self, "config_read_error", None) is not None:
            return False
        if self._access_token() or self.online.get("username") or self.online.get("email"):
            return False
        if not self.online.get("guest_id"):
            return False
        if self.online.get("account_kind") not in (None, "guest"):
            return False

        previous_online = self.online
        replacement = dict(previous_online)
        for key in (
            "auth",
            "guest_id",
            "account_kind",
            "display_name",
            "group",
            "members",
            "published_deck",
            "last_error",
        ):
            replacement.pop(key, None)
        self._cancel_identity_bootstrap()
        self.online = replacement
        self._member_cache_key = None
        self._last_member_fetch_at = 0
        self.apply_room_time_zone(DEFAULT_TIME_ZONE)
        try:
            self.save()
        except Exception:
            self.online = previous_online
            raise
        self.refresh()
        self.ensure_online_identity()
        return True

    def _run_online_action(self, controls, task, success, error_prefix, *, on_error=None):
        generation = self.identity_generation
        for control in controls:
            control.setEnabled(False)

        def done(future):
            if self.closed or generation != self.identity_generation:
                return
            for control in controls:
                try:
                    control.setEnabled(True)
                except RuntimeError:
                    pass
            try:
                success(future.result())
            except SupabaseError as error:
                (on_error or showWarning)(f"{error_prefix}\n{error}")
            except Exception as error:
                (on_error or showWarning)(f"{error_prefix}\n{error}")

        mw.taskman.run_in_background(task, done)

    def _run_authenticated_action(self, controls, operation, success, error_prefix, *, on_error=None):
        for control in controls:
            control.setEnabled(False)
        auth = dict(self.online.get("auth") or {})
        starting_access_token = auth.get("access_token")
        generation = self.identity_generation

        def refresh_auth():
            refresh_token = auth["refresh_token"]
            cache_key = (auth.get("user_id"), refresh_token)
            refresh_lock = getattr(self, "_auth_refresh_lock", None)
            if refresh_lock is None:
                refresh_lock = self._auth_refresh_lock = threading.Lock()
            with refresh_lock:
                cache = getattr(self, "_auth_refresh_cache", None)
                if cache is None:
                    cache = self._auth_refresh_cache = {}
                refreshed_auth = cache.get(cache_key)
                if refreshed_auth is None:
                    try:
                        refreshed = self.client.refresh(refresh_token)
                    except SupabaseError as error:
                        if error.status in (400, 401):
                            raise SupabaseError("로그인이 만료되었습니다.", status=401) from error
                        raise
                    refreshed_auth = {
                        "access_token": refreshed["access_token"],
                        "refresh_token": refreshed.get("refresh_token", refresh_token),
                        "expires_at": refreshed.get(
                            "expires_at", int(time.time()) + refreshed.get("expires_in", 3600)
                        ),
                    }
                    cache[cache_key] = dict(refreshed_auth)
            auth.update(refreshed_auth)

        def task():
            try:
                if getattr(self, "_auth_persistence_error", None) is not None:
                    raise SupabaseError(
                        "로그인 정보를 저장하지 못했습니다. 저장 문제를 해결한 뒤 다시 시도해 주세요."
                    )
                if not auth.get("access_token"):
                    raise SupabaseError("로그인 정보가 없습니다.", status=401)
                if auth.get("refresh_token") and time.time() >= auth.get("expires_at", 0) - 60:
                    refresh_auth()
                try:
                    result = operation(auth["access_token"])
                except SupabaseError as error:
                    if error.status != 401 or not auth.get("refresh_token"):
                        raise
                    refresh_auth()
                    result = operation(auth["access_token"])
                return auth, result, None
            except Exception as error:
                return auth, None, error

        def done(future):
            if (self.closed or generation != self.identity_generation
                    or self.online.get("auth", {}).get("user_id") != auth.get("user_id")):
                return
            for control in controls:
                try:
                    control.setEnabled(True)
                except RuntimeError:
                    pass
            try:
                updated_auth, result, operation_error = future.result()
                current_auth = self.online.get("auth") or {}
                if current_auth.get("access_token") not in (
                    starting_access_token, updated_auth.get("access_token")
                ):
                    return
                if (
                    updated_auth.get("access_token") != starting_access_token
                    and current_auth.get("access_token") in (
                        starting_access_token, updated_auth.get("access_token")
                    )
                ):
                    self.online["auth"] = updated_auth
                    self.online.pop("last_error", None)
                    try:
                        self.save()
                    except Exception as error:
                        self._auth_persistence_error = error
                        raise
                if operation_error is not None:
                    raise operation_error
                success(result)
            except SupabaseError as error:
                if error.status == 401:
                    self.online.pop("auth", None)
                    self.online.pop("members", None)
                    self.online["last_error"] = "로그인이 만료되었습니다. 다시 로그인해 주세요."
                    self.save()
                (on_error or showWarning)(f"{error_prefix}\n{error}")
            except Exception as error:
                (on_error or showWarning)(f"{error_prefix}\n{error}")

        mw.taskman.run_in_background(task, done)

    def _current_member_status(self, member):
        return member_status(member, datetime.now(timezone.utc))

    def _access_token(self):
        return self.online.get("auth", {}).get("access_token")

    def _store_session(self, result, email, display_name):
        user = result.get("user") or {}
        user_id = user.get("id") or result.get("user_id")
        self.online.update(
            {
                "email": email,
                "display_name": display_name,
                "auth": {
                    "access_token": result["access_token"],
                    "refresh_token": result.get("refresh_token", ""),
                    "expires_at": result.get("expires_at", int(time.time()) + result.get("expires_in", 3600)),
                    "user_id": user_id,
                },
                "guest_id": user_id,
            }
        )

    def discard_room_outbox(self, user_id, group_id):
        """Forget only snapshots belonging to a room the account has left."""
        removed = self.sync_outbox.discard_room(str(user_id), str(group_id))
        self.review_history.invalidate_route(str(user_id), str(group_id))
        published_deck = self.online.get("published_deck")
        if isinstance(published_deck, dict) and published_deck.get("group_id") == str(group_id):
            self.online.pop("published_deck", None)
        self.save()
        return removed

    def leave_current_room_locally(self, user_id, group_id):
        """Clear only the matching account/room after a confirmed remote leave."""
        auth = self.online.get("auth") or {}
        group = self.online.get("group") or {}
        if str(auth.get("user_id") or "") != str(user_id):
            return False
        if str(group.get("id") or "") != str(group_id):
            return False
        removed = self.sync_outbox.discard_room(str(user_id), str(group_id))
        self.review_history.invalidate_route(str(user_id), str(group_id))
        published_deck = self.online.get("published_deck")
        if isinstance(published_deck, dict) and published_deck.get("group_id") == str(group_id):
            self.online.pop("published_deck", None)
        self.online.pop("group", None)
        self.online.pop("members", None)
        self._member_cache_key = None
        self._last_member_fetch_at = 0
        self.apply_room_time_zone(DEFAULT_TIME_ZONE)
        self.save()
        self.refresh()
        return True

    def sync_async(self, force=False):
        from datetime import timedelta
        import time as clock_module
        from .study_day import (
            DEFAULT_TIME_ZONE,
            room_time_zone,
            study_day,
        )
        from .room_activity import (
            missing_week_days,
            presence_status,
            prune_week_cache,
            shareable_deck_name,
            store_week_day,
        )

        if self.closed or getattr(self, "config_read_error", None) is not None:
            return
        retry_clock = clock_module.monotonic()
        if (
            not force
            and retry_clock < float(getattr(self, "next_sync_attempt_at", 0) or 0)
        ):
            return

        def schedule_retry():
            failures = int(getattr(self, "sync_failure_count", 0) or 0) + 1
            self.sync_failure_count = failures
            retry_delay = min(300, 30 * (2 ** min(failures, 4)))
            self.next_sync_attempt_at = clock_module.monotonic() + retry_delay

        def clear_retry():
            self.sync_failure_count = 0
            self.next_sync_attempt_at = 0.0
        if self.sync_in_flight:
            if force:
                self.sync_pending = True
            return
        if not self._access_token():
            return
        sync_generation = self.identity_generation
        auth = dict(self.online["auth"])
        session_token = auth.get("access_token")
        sanitize = globals().get("sanitize_display_name")
        if sanitize is not None:
            display_name = sanitize(
                self.online.get("display_name"),
                fallback_user_id=auth["user_id"],
            )
        else:
            display_name = canonical_nickname(auth["user_id"])
        update_name = self.online.get("synced_display_name") != display_name
        group = dict(self.online.get("group") or {})
        group_id = group.get("id")
        current = now()
        apply_time_zone = getattr(self, "apply_room_time_zone", None)
        if apply_time_zone is not None:
            apply_time_zone(room_time_zone(group), current)
        time_zone = getattr(self.tracker, "time_zone", DEFAULT_TIME_ZONE)
        study_day_key = study_day(current, time_zone).isoformat()
        record = self.tracker.today(current)
        payloads = []
        recent_review_day = (
            study_day(current, time_zone) - timedelta(days=1)
        ).isoformat()
        review_payloads = (
            self.review_history.pending(
                auth["user_id"], group_id, since_day=recent_review_day
            )
            if group else []
        )
        review_acks = []
        stage_errors = {}
        # Keep the last opened deck visible while connected, until it changes;
        # never carry a deck from a previous room day.
        current_deck_name = shareable_deck_name(
            bool(self.online.get("share_deck_name", True)),
            self.tracker.current_deck_name,
            getattr(self.tracker, "current_deck_day", None),
            study_day_key,
        )
        published_status = presence_status(
            self.tracker.status, getattr(self.tracker, "last_input_at", None), current
        )
        published_deck = self.online.get("published_deck")
        published_deck_matches = isinstance(published_deck, dict) and all(
            published_deck.get(key) == value
            for key, value in {
                "user_id": auth["user_id"],
                "group_id": group_id,
                "device_id": self.device_id,
                "name": current_deck_name,
            }.items()
        )
        deck_published_at = (
            float(published_deck.get("published_at", 0) or 0)
            if isinstance(published_deck, dict) else 0
        )
        clock_now = clock_module.time()
        deck_publish_age = clock_now - deck_published_at
        publish_deck = bool(group) and (
            not published_deck_matches
            or (
                current_deck_name is not None
                # get_group_current_decks drops decks older than 90 seconds; with the
                # 30-second sync cadence a 45-second refresh keeps them visible.
                and (deck_publish_age < 0 or deck_publish_age >= 45)
            )
        )
        member_cache_key = (auth["user_id"], group_id, study_day_key)
        last_member_fetch = float(getattr(self, "_last_member_fetch_at", 0) or 0)
        member_fetch_age = clock_now - last_member_fetch
        panel = getattr(self, "panel", None)
        panel_visible = panel is None or panel.isVisible()
        fetch_members = bool(group) and panel_visible and (
            force
            or getattr(self, "_member_cache_key", None) != member_cache_key
            or not isinstance(self.online.get("members"), list)
            or member_fetch_age < 0
            or member_fetch_age >= 90
        )
        # Finished days no longer change: each is read once per room and kept
        # while it is in the 7-day window. Reads ride on a visible-panel member
        # refresh only, so a hidden panel never fetches them.
        week_cache = self.online.get("room_week_stats")
        if not isinstance(week_cache, dict):
            week_cache = self.online["room_week_stats"] = {}
        prune_week_cache(week_cache, study_day(current, time_zone))
        week_fetch_days = (
            missing_week_days(week_cache, group_id, study_day(current, time_zone))
            if fetch_members else []
        )
        week_results = {}
        # Pokes are best-effort: fetched every sync while in a room (also with
        # the panel hidden, since the tooltip is the notification), skipped for
        # a while once the server is known to lack the RPC, never an error.
        poke_fetcher = getattr(getattr(self, "client", None), "fetch_my_pokes", None)
        fetch_pokes = (
            bool(group)
            and callable(poke_fetcher)
            and retry_clock >= float(getattr(self, "_pokes_unavailable_until", 0) or 0)
        )
        poke_results = []
        poke_state = {}
        if group:
            previous_route = self.sync_outbox.route(self.device_id)
            # Local Anki totals are shared across rooms. Namespace the device
            # ledger by room so joining another room starts at the current
            # local totals instead of replaying study from the previous room.
            same_route = (
                previous_route
                and previous_route.get("user_id") == auth["user_id"]
                and previous_route.get("group_id") == group_id
            )
            ledger_id = (
                previous_route.get("ledger_id")
                if same_route and previous_route.get("ledger_id")
                else f'{auth["user_id"]}|{group_id}'
            )
            # Preserve the already-deployed account/day ledger for its first
            # room. Once a route is bound, later rooms always get a separate
            # stream and cannot inherit this room's counters.
            if (
                not previous_route
                and auth["user_id"] in self.device_ledger.state.get("accounts", {})
                and self.sync_outbox.claim_legacy_ledger(
                    user_id=auth["user_id"], group_id=group_id,
                    device_id=self.device_id,
                )
            ):
                ledger_id = auth["user_id"]
            if (
                same_route
                and previous_route.get("study_day") != study_day_key
            ):
                previous_day = previous_route["study_day"]
                previous_record = self.tracker.records.get(previous_day)
                if previous_record is not None:
                    previous_snapshot = self.device_ledger.prepare(
                        user_id=ledger_id,
                        day=previous_day,
                        active_seconds=int(previous_record.get("seconds", 0)),
                        answer_count=int(previous_record.get("answers", 0)),
                        time_goal_minutes=self.tracker.time_goal_minutes,
                        card_goal=self.tracker.card_goal,
                        status="stopped",
                    )
                    previous_payload = {
                        "user_id": auth["user_id"],
                        "group_id": group_id,
                        "device_id": self.device_id,
                        "study_day": previous_day,
                        "ledger_id": ledger_id,
                        **previous_snapshot,
                    }
                    self.sync_outbox.enqueue(previous_payload)
            device_snapshot = self.device_ledger.prepare(
                user_id=ledger_id,
                day=study_day_key,
                active_seconds=int(record["seconds"]),
                answer_count=int(record["answers"]),
                time_goal_minutes=self.tracker.time_goal_minutes,
                card_goal=self.tracker.card_goal,
                status=published_status,
            )
            payload = {
                "user_id": auth["user_id"],
                "group_id": group["id"],
                "device_id": self.device_id,
                "study_day": study_day_key,
                "ledger_id": ledger_id,
                **device_snapshot,
            }
            self.sync_outbox.enqueue(payload)
            self.sync_outbox.bind_route(
                user_id=auth["user_id"], group_id=group_id,
                device_id=self.device_id, study_day=study_day_key, ledger_id=ledger_id,
            )
            payloads = self.sync_outbox.pending(
                user_id=auth["user_id"], group_id=group_id,
                device_id=self.device_id,
            )
            sync_route_epoch = self.sync_outbox.route_epoch(self.device_id)
            # The revision and its exact cumulative snapshot must survive a
            # crash before the request; retries then remain idempotent.
            try:
                self.save()
            except OSError:
                self.online["last_error"] = self.t(
                    "로컬 기록을 저장하지 못해 전송을 보류했습니다. 저장 공간과 폴더 권한을 확인해 주세요.",
                    "Upload paused because local records could not be saved. Check disk space and folder permissions.",
                )
                return
        else:
            sync_route_epoch = None

        recovery_baselines = {
            item["study_day"]: dict(self.tracker.records.get(item["study_day"], {}))
            for item in payloads
        }
        def task():
            if auth.get("refresh_token") and time.time() >= auth.get("expires_at", 0) - 60:
                try:
                    refreshed = self.client.refresh(auth["refresh_token"])
                except SupabaseError as error:
                    if error.status in (400, 401):
                        raise SupabaseError(
                            "로그인이 만료되었습니다.", status=401
                        ) from error
                    raise
                auth.update(
                    {
                        "access_token": refreshed["access_token"],
                        "refresh_token": refreshed.get("refresh_token", auth["refresh_token"]),
                        "expires_at": refreshed.get(
                            "expires_at", int(time.time()) + refreshed.get("expires_in", 3600)
                        ),
                    }
                )
            token = auth["access_token"]
            def upload(token):
                stage_errors.clear()
                review_acks.clear()
                if update_name:
                    self.client.upsert_profile(token, auth["user_id"], display_name)
                acknowledgements = []
                first_error = None
                ordered_review_payloads = sorted(
                    review_payloads,
                    key=lambda batch: (
                        batch.get("target_day") != study_day_key,
                        str(batch.get("target_day", "")),
                    ),
                )
                for batch in ordered_review_payloads:
                    try:
                        self.client.sync_review_day(token, group_id=group_id, batch=batch)
                        review_acks.append(batch)
                    except SupabaseError as error:
                        if error.status == 401:
                            raise
                        if first_error is None:
                            first_error = error
                        if _continue_after_review_batch_error(error):
                            continue
                        break
                    except Exception as error:
                        first_error = error
                        break
                for queued in payloads:
                    try:
                        acknowledgement = self.client.record_device_day(
                            token,
                            group_id=queued["group_id"],
                            device_id=queued["device_id"],
                            study_day=queued["study_day"],
                            revision=queued["revision"],
                            active_seconds=queued["active_seconds"],
                            answer_count=queued["answer_count"],
                            time_goal_minutes=queued["time_goal_minutes"],
                            card_goal=queued["card_goal"],
                            status=queued["status"],
                        )
                        acknowledgements.append((queued, acknowledgement["revision"]))
                    except DeviceSnapshotConflict as error:
                        acknowledgements.append((queued, error))
                    except SupabaseError as error:
                        if error.status == 401:
                            raise
                        if first_error is None:
                            first_error = error
                    except Exception as error:
                        if first_error is None:
                            first_error = error
                deck_published = False
                if publish_deck:
                    try:
                        self.client.set_current_deck(token, group_id, self.device_id, current_deck_name)
                        deck_published = True
                    except SupabaseError as error:
                        if error.status == 401:
                            raise
                        if first_error is None:
                            first_error = error
                members = None
                if first_error is not None:
                    stage_errors["upload"] = first_error
                if fetch_members:
                    try:
                        members = self.client.fetch_group_today(token, group_id, study_day_key)
                    except SupabaseError as error:
                        stage_errors["members"] = error
                        if error.status == 401:
                            raise
                        if first_error is None:
                            first_error = error
                    except Exception as error:
                        stage_errors["members"] = error
                        # Upload acknowledgements remain valid even when the
                        # independent member-list read fails afterwards.
                        if first_error is None:
                            first_error = error
                week_results.clear()
                for week_day in week_fetch_days if members is not None else []:
                    try:
                        week_results[week_day] = self.client.fetch_group_day_stats(
                            token, group_id, week_day
                        )
                    except SupabaseError as error:
                        if error.status == 401:
                            raise
                        # The day stays uncached and is drawn as a gap; the
                        # next member refresh retries it.
                    except Exception:
                        pass
                poke_results.clear()
                poke_state.clear()
                if fetch_pokes:
                    try:
                        poke_results.extend(poke_fetcher(token, group_id) or [])
                    except SupabaseError as error:
                        # 404 / PGRST202: the poke migration is not applied.
                        if error.status == 404:
                            poke_state["unavailable"] = True
                    except Exception:
                        pass
                return acknowledgements, members, deck_published, first_error
            try:
                acknowledgements, members, deck_published, upload_error = upload(token)
            except SupabaseError as error:
                if error.status != 401 or not auth.get("refresh_token"):
                    return auth, [], None, False, error
                try:
                    refreshed = self.client.refresh(auth["refresh_token"])
                    auth.update(
                        {
                            "access_token": refreshed["access_token"],
                            "refresh_token": refreshed.get(
                                "refresh_token", auth["refresh_token"]
                            ),
                            "expires_at": refreshed.get(
                                "expires_at",
                                int(time.time()) + refreshed.get("expires_in", 3600),
                            ),
                        }
                    )
                    token = auth["access_token"]
                    acknowledgements, members, deck_published, upload_error = upload(token)
                except SupabaseError as refresh_error:
                    if refresh_error.status in (400, 401):
                        refresh_error = SupabaseError("로그인이 만료되었습니다.", status=401)
                    return auth, [], None, False, refresh_error
            except Exception as error:
                return auth, [], None, False, error
            return auth, acknowledgements, members, deck_published, upload_error

        def done(future):
            self.sync_in_flight = False
            current_group_id = (self.online.get("group") or {}).get("id")
            current_token = (self.online.get("auth") or {}).get("access_token")
            current_route_epoch = self.sync_outbox.route_epoch(self.device_id)
            if (
                self.closed
                or self.identity_generation != sync_generation
                or self.online.get("auth", {}).get("user_id") != auth["user_id"]
                or current_token != session_token
                or current_group_id != group_id
                or getattr(self.tracker, "time_zone", DEFAULT_TIME_ZONE) != time_zone
                or (group_id and current_route_epoch != sync_route_epoch)
            ):
                if self.sync_pending and not self.closed:
                    self.sync_pending = False
                    QTimer.singleShot(0, lambda: self.sync_async(force=True))
                return
            try:
                (
                    updated_auth,
                    acknowledgements,
                    members,
                    deck_published,
                    sync_error,
                ) = future.result()
                self.online["auth"] = updated_auth
                if sync_error is not None and _not_group_member_error(sync_error):
                    try:
                        from .sync import handle_kicked_state
                        handle_kicked_state(self, auth["user_id"], group_id)
                    except Exception:
                        self.online["recovery_notice"] = self.t(
                            "방에서 내보내졌습니다. 방 연결이 초기화되었습니다.",
                            "You were removed from the room. Room connection has been reset.",
                        )
                        self.leave_current_room_locally(auth["user_id"], group_id)
                    return
                for batch in review_acks:
                    self.review_history.acknowledge(auth["user_id"], group_id, batch)
                self.review_history.compact(
                    (
                        study_day(current, time_zone)
                        - timedelta(days=2)
                    ).isoformat()
                )
                for acknowledged_payload, acknowledged_revision in acknowledgements:
                    if isinstance(acknowledged_revision, DeviceSnapshotConflict):
                        baseline = recovery_baselines[acknowledged_payload["study_day"]]
                        stored = acknowledged_revision.stored
                        if stored.get("user_id") != auth["user_id"]:
                            raise SupabaseError("복원 기록 계정이 일치하지 않습니다. / Restored account mismatch.")
                        self.device_ledger.rebase_from_server(
                            user_id=acknowledged_payload.get("ledger_id", auth["user_id"]),
                            day=acknowledged_payload["study_day"],
                            active_seconds=int(baseline.get("seconds", 0)),
                            answer_count=int(baseline.get("answers", 0)), stored=stored,
                        )
                        self.sync_outbox.resolve_server_conflict(acknowledged_payload, stored)
                        self.online["recovery_notice"] = self.t(
                            "서버 기록을 기준으로 다시 연결했습니다. 복원 후 재연결 전 기록은 중복 여부를 확인할 수 없어 자동 합치지 않았습니다.",
                            "Reconnected using server totals. Study between restoration and reconnection was not merged because duplicates could not be ruled out.",
                        )
                        continue
                    self.device_ledger.acknowledge(
                        acknowledged_payload.get("ledger_id", auth["user_id"]),
                        acknowledged_payload["study_day"],
                        acknowledged_revision,
                    )
                    self.sync_outbox.acknowledge(
                        acknowledged_payload, acknowledged_revision
                    )
                self.online["display_name"] = display_name
                self.online["synced_display_name"] = display_name
                if deck_published:
                    self.online["published_deck"] = {
                        "user_id": auth["user_id"],
                        "group_id": group_id,
                        "device_id": self.device_id,
                        "name": current_deck_name,
                        "published_at": clock_module.time(),
                    }
                if members is not None:
                    self.online["members"] = members
                    self._member_cache_key = member_cache_key
                    self._last_member_fetch_at = clock_module.time()
                if poke_state.get("unavailable"):
                    from .pokes import POKE_UNAVAILABLE_RETRY_SECONDS
                    self._pokes_unavailable_until = (
                        clock_module.monotonic() + POKE_UNAVAILABLE_RETRY_SECONDS
                    )
                if poke_results:
                    show_pokes = getattr(self, "show_pokes", None)
                    if callable(show_pokes):
                        try:
                            show_pokes(list(poke_results))
                        except Exception:
                            pass
                if week_results:
                    stored_weeks = self.online.get("room_week_stats")
                    if not isinstance(stored_weeks, dict):
                        stored_weeks = self.online["room_week_stats"] = {}
                    for week_day, rows in week_results.items():
                        store_week_day(stored_weeks, group_id, week_day, rows)
                status_ledger = getattr(self, "record_status", None)
                upload_writes_complete = (
                    len(acknowledgements) == len(payloads)
                    and len(review_acks) == len(review_payloads)
                )
                upload_complete = (
                    upload_writes_complete
                    and (not publish_deck or deck_published)
                    and (sync_error is None or "members" in stage_errors)
                    and "upload" not in stage_errors
                )
                if status_ledger is not None and group_id:
                    if upload_complete:
                        status_ledger.mark_success(
                            "upload", user_id=auth["user_id"], group_id=group_id
                        )
                    elif sync_error is not None:
                        status_ledger.set_error(
                            "upload", sync_error,
                            user_id=auth["user_id"], group_id=group_id,
                        )
                    if fetch_members and members is not None:
                        status_ledger.mark_success(
                            "members", user_id=auth["user_id"], group_id=group_id
                        )
                    elif "members" in stage_errors:
                        status_ledger.set_error(
                            "members", stage_errors["members"],
                            user_id=auth["user_id"], group_id=group_id,
                        )
                if sync_error is not None:
                    self.online["last_error"] = str(sync_error)
                    schedule_retry()
                else:
                    self.online.pop("last_error", None)
                    clear_retry()
                self.save()
            except SupabaseError as error:
                if error.status == 401:
                    self.online.pop("auth", None)
                    self.online.pop("members", None)
                    self.online["last_error"] = "로그인이 만료되었습니다. 다시 로그인해 주세요."
                else:
                    self.online["last_error"] = str(error)
                status_ledger = getattr(self, "record_status", None)
                if status_ledger is not None and group_id:
                    status_ledger.set_error(
                        "upload", error,
                        user_id=auth["user_id"], group_id=group_id,
                    )
                schedule_retry()
                self.save()
            except OSError as error:
                self.online["last_error"] = str(error)
                status_ledger = getattr(self, "record_status", None)
                if status_ledger is not None:
                    status_ledger.set_error("local_save", error)
                schedule_retry()
            except Exception as error:
                self.online["last_error"] = str(error)
                status_ledger = getattr(self, "record_status", None)
                if status_ledger is not None and group_id:
                    status_ledger.set_error(
                        "upload", error,
                        user_id=auth["user_id"], group_id=group_id,
                    )
                schedule_retry()
                self.save()
            if self.sync_pending:
                self.sync_pending = False
                QTimer.singleShot(0, lambda: self.sync_async(force=True))

        self.sync_in_flight = True
        try:
            mw.taskman.run_in_background(task, done)
        except RuntimeError:
            self.sync_in_flight = False
            schedule_retry()
            self.online["last_error"] = self.t(
                "동기화를 시작하지 못했습니다. 잠시 후 자동으로 다시 시도합니다.",
                "Sync could not start. It will retry automatically after a delay.",
            )
            status_ledger = getattr(self, "record_status", None)
            if status_ledger is not None and group_id:
                status_ledger.set_error(
                    "upload", self.online["last_error"],
                    user_id=auth["user_id"], group_id=group_id,
                )


controller = None


def profile_opened():
    global controller
    if controller is not None:
        controller.close()
    controller = Controller()


def profile_closing():
    global controller
    if controller is not None:
        controller.close()
        controller = None


def show_question(card):
    if controller is not None:
        controller.show_question(card)


def answer_card(reviewer, card, ease):
    if controller is not None:
        controller.answer(reviewer, card, ease)


def state_changed(new_state, old_state):
    if controller is not None:
        controller.state_changed(new_state, old_state)


gui_hooks.profile_did_open.append(profile_opened)
gui_hooks.profile_will_close.append(profile_closing)
gui_hooks.reviewer_did_show_question.append(show_question)
gui_hooks.reviewer_did_answer_card.append(answer_card)
gui_hooks.state_did_change.append(state_changed)


def review_history_changed(changes, *args):
    if controller is not None:
        controller.review_dirty = True
        # Anki has state_did_undo but no matching redo hook. Local card
        # operations include redo; sync/reset observations must not erase or
        # revive records merely because a different snapshot was downloaded.
        if getattr(changes, "card", False) and not controller.review_syncing:
            controller.review_allow_removals = True


def review_sync_started():
    if controller is not None:
        controller.review_syncing = True
        controller.review_allow_removals = False


def review_sync_finished():
    if controller is not None:
        controller.review_syncing = False
        controller.review_dirty = True
        controller.review_upload_requested = True
        controller.refresh_review_history()


def review_undone(*args):
    if controller is not None:
        controller.review_allow_removals = True
        controller.review_dirty = True
        controller.review_upload_requested = True


gui_hooks.operation_did_execute.append(review_history_changed)
gui_hooks.sync_will_start.append(review_sync_started)
gui_hooks.sync_did_finish.append(review_sync_finished)
gui_hooks.state_did_undo.append(review_undone)
