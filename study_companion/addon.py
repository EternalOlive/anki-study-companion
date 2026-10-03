"""PC Anki integration for the local study tracker."""

from __future__ import annotations

import json
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
from aqt.utils import showWarning

from .online import (
    DeviceSyncLedger,
    DeviceSnapshotConflict,
    SupabaseClient,
    SupabaseError,
    load_or_create_device_id,
    profile_device_id,
)
from .nicknames import canonical_nickname, localize_nickname, disambiguate_nickname
from .outbox import SyncOutbox
from .activity import weekly_activity
from .reviews import ReviewHistory
from .panel import PanelToggleButton, StudyPanel
from .tracker import (
    StudyTracker,
    TIMEZONE,
)


def now() -> datetime:
    return datetime.now(TIMEZONE)


class InputWatcher(QObject):
    def __init__(self, controller):
        super().__init__(mw)
        self.controller = controller

    def eventFilter(self, watched, event):
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
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            data = {}
        self.tracker = StudyTracker(
            records=data.get("records"),
            deck_records=data.get("deck_records"),
            time_goal_minutes=data.get("time_goal_minutes", 0),
            card_goal=data.get("card_goal", 0),
        )
        self.online = data.get("online", {})
        self.review_history = ReviewHistory(data.get("review_history", {}))
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
        self.locale = data.get("language", "ko" if lang.current_lang.startswith("ko") else "en")
        if self.locale not in ("ko", "en"):
            self.locale = "ko"
        self.ui_state = data.get("ui_state", {})
        self.client = SupabaseClient()
        self.sync_in_flight = False
        self.sync_pending = False
        self.sync_failure_count = 0
        self.next_sync_attempt_at = 0.0
        self._member_cache_key = None
        self._last_member_fetch_at = 0
        self.identity_in_flight = False
        self.identity_generation = 0
        self.closed = False
        self.label = QLabel(mw)
        mw.statusBar().addPermanentWidget(self.label)
        self.action = mw.form.menuTools.addAction("스터디 현황")
        self.action.triggered.connect(self.show_dialog)
        self._build_side_panel()
        self.watcher = InputWatcher(self)
        QApplication.instance().installEventFilter(self.watcher)
        self.timer = QTimer(mw)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.ticks_since_save = 0
        self.refresh()
        QTimer.singleShot(0, self.refresh_review_history)
        QTimer.singleShot(1000, self.ensure_online_identity)

    def close(self):
        self.closed = True
        settings_dialog = getattr(self, "_settings_dialog", None)
        if settings_dialog is not None:
            settings_dialog.reject()
        self._cancel_identity_bootstrap()
        self.tracker.leave_review(now())
        self.save()
        self.timer.stop()
        QApplication.instance().removeEventFilter(self.watcher)
        mw.statusBar().removeWidget(self.label)
        mw.statusBar().removeWidget(self.panel_expand)
        self.panel_expand.deleteLater()
        mw.form.menuTools.removeAction(self.action)
        mw.form.menuTools.removeAction(self.panel_action)
        mw.removeDockWidget(self.panel)
        self.panel.deleteLater()

    def save(self):
        data = self.tracker.snapshot()
        data["online"] = self.online
        data["language"] = self.locale
        data["ui_state"] = self.ui_state
        data["review_history"] = self.review_history.state
        payload = json.dumps(data, ensure_ascii=False)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(self.path)

    def tick(self):
        self.tracker.tick(now())
        if self.review_dirty or self.review_history.today(now().date().isoformat()) is None:
            self.refresh_review_history()
        self.ticks_since_save += 1
        if self.ticks_since_save >= 30:
            self.save()
            self.sync_async()
            self.ticks_since_save = 0
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
        self.tracker.answer(now())
        self.review_dirty = True
        self.refresh_review_history()
        self.save()
        self.refresh()

    def state_changed(self, new_state, old_state):
        if old_state == "review" and new_state != "review":
            self.tracker.leave_review(now())
            self.save()
            self.refresh()
            self.sync_async(force=True)

    def refresh(self):
        record = self.study_record(now())
        duration = self.panel_body.format_clock(int(record["seconds"]))
        status = {"studying": self.t("공부 중", "Studying"), "paused": self.t("잠시 멈춤", "Paused"), "stopped": self.t("접속 중", "Online")}[
            self.tracker.status
        ]
        time_goal = f"/{self.panel_body.format_clock(self.tracker.time_goal_minutes * 60)}" if self.tracker.time_goal_minutes else ""
        card_goal = f"/{self.tracker.card_goal}" if self.tracker.card_goal else ""
        self.label.setText(
            f"{self.t('오늘', 'Today')} {duration}{time_goal}  "
            f"{self.t('답변', 'Answers')} {record['answers']}{card_goal}  {status}"
        )
        self.refresh_panel()

    def study_record(self, current):
        return self.review_history.today(current.date().isoformat()) or {"seconds": 0, "answers": 0}

    def weekly_record(self, current):
        snapshot = self._weekly_activity
        if not isinstance(snapshot, dict):
            return None
        if snapshot.get("day") != current.date().isoformat():
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

        current = now()
        today = current.date().isoformat()
        today_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        start = today_start - timedelta(days=13)
        yesterday = (today_start - timedelta(days=1)).date().isoformat()
        start_ms = int(start.timestamp() * 1000)
        end_ms = int((today_start + timedelta(days=1)).timestamp() * 1000)
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
            if aggregate_current.date() != current.date():
                # Keep the snapshot on the queried day. The next timer tick
                # immediately issues the new day's correctly bounded query.
                aggregate_current = current
            return (
                str(col.crt), id(col), rows,
                weekly_activity(rows, aggregate_current),
            )

        def success(result):
            self.review_query_in_flight = False
            if self.closed:
                return
            collection_key, collection_identity, rows, weekly = result
            rows_by_day = {yesterday: [], today: []}
            for row in rows:
                try:
                    row_day = datetime.fromtimestamp(int(row[0]) / 1000, TIMEZONE).date().isoformat()
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
                (current.date() - timedelta(days=2)).isoformat(),
                discard_unrouted=not bool(group),
            )
            self._weekly_activity = {
                "day": today,
                "collection_key": str(collection_key),
                "collection_identity": collection_identity,
                "record": weekly,
            }
            self.online.pop("review_error", None)
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

        try:
            QueryOp(parent=mw, op=query, success=success).failure(failure).run_in_background()
        except Exception as error:
            failure(error)

    def t(self, korean, english):
        return english if self.locale == "en" else korean

    def set_locale(self, locale):
        self.locale = locale if locale in ("ko", "en") else "ko"
        self.save()
        self.refresh()

    def display_member_name(self, member):
        name = member.get("display_name") or canonical_nickname(member.get("user_id") or "local")
        display = localize_nickname(name, self.locale)
        duplicates = {
            item.get("user_id") for item in self.online.get("members", [])
            if item.get("display_name") == name and item.get("user_id")
        }
        if len(duplicates) > 1:
            display = disambiguate_nickname(display, member["user_id"], True)
        return display

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
        title_layout.setContentsMargins(2, 0, 2, 0)
        title_layout.addStretch()
        # Keep the collapse control outside scrollable content, even in a
        # narrow window where the study details need horizontal scrolling.
        title_layout.addWidget(self.panel_body.collapse_panel)
        self.panel.setTitleBarWidget(title_bar)
        self.panel_scroll = QScrollArea(self.panel)
        self.panel_scroll.setWidgetResizable(True)
        self.panel_scroll.setMinimumWidth(0)
        self.panel_scroll.setWidget(self.panel_body)
        self.panel.setWidget(self.panel_scroll)
        mw.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.panel)
        self.panel_expand = PanelToggleButton(mw, expand=True)
        self.panel_expand.clicked.connect(lambda: self.set_panel_collapsed(False))
        mw.statusBar().addPermanentWidget(self.panel_expand)
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

    def set_panel_collapsed(self, collapsed, *, persist=True):
        collapsed = bool(collapsed)
        was_hidden = not self.panel.isVisible()
        if collapsed:
            current_width = self.panel.width()
            if self.panel.isVisible() and current_width >= 160:
                self.ui_state["panel_width"] = current_width
            self.panel.hide()
            self.panel_expand.show()
        else:
            self.panel.setMaximumWidth(16777215)
            self.panel.setMinimumWidth(0)
            self.panel_body.set_collapsed(False)
            self.panel.setWindowTitle(self.t("스터디", "Study"))
            target_width = min(max(160, int(self.ui_state.get("panel_width") or 320)),
                               max(160, mw.width() // 2))
            self.panel.show()
            self.panel_expand.hide()
            mw.resizeDocks([self.panel], [target_width], Qt.Orientation.Horizontal)
            self.panel_body.refresh()
        self.ui_state["panel_collapsed"] = collapsed
        if persist:
            self.save()
        if not collapsed and was_hidden and persist:
            self.sync_async(force=True)

    def refresh_panel(self):
        self.action.setText(self.t("스터디 관리", "Study settings"))
        self.panel.setWindowTitle(
            "" if self.panel_body.collapsed else self.t("스터디", "Study")
        )
        self.panel_action.setText(self.t("스터디 패널", "Study panel"))
        self.panel_expand.setToolTip(self.t("스터디 패널 펼치기", "Show study panel"))
        self.panel_expand.setAccessibleName(self.panel_expand.toolTip())
        if self.panel.isVisible():
            self.panel_body.refresh()

    def show_dialog(self):
        if self.closed:
            return
        existing = getattr(self, "_settings_dialog", None)
        if existing is not None:
            existing.raise_()
            existing.activateWindow()
            return
        from .settings import SettingsDialog
        dialog = SettingsDialog(self, mw)
        self._settings_dialog = dialog
        try:
            dialog.exec()
        finally:
            self._settings_dialog = None
            dialog.deleteLater()

    def ensure_online_identity(self):
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
        generation = self.identity_generation

        def refresh_auth():
            try:
                refreshed = self.client.refresh(auth["refresh_token"])
            except SupabaseError as error:
                if error.status in (400, 401):
                    raise SupabaseError("로그인이 만료되었습니다.", status=401) from error
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

        def task():
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
            return auth, result

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
                updated_auth, result = future.result()
                self.online["auth"] = updated_auth
                self.online.pop("last_error", None)
                self.save()
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
        status = member.get("status", "stopped")
        updated_at = member.get("updated_at")
        if not updated_at:
            return "offline"
        try:
            updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - updated.astimezone(timezone.utc) > timedelta(seconds=90):
                return "offline"
        except (TypeError, ValueError):
            return "offline"
        return status if status in ("studying", "paused", "online") else "online"

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

    def sync_async(self, force=False):
        from datetime import timedelta
        import time as clock_module

        if self.closed:
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
        display_name = canonical_nickname(auth["user_id"])
        update_name = self.online.get("display_name") != display_name
        group = dict(self.online.get("group") or {})
        group_id = group.get("id")
        current = now()
        study_day = current.date().isoformat()
        record = self.tracker.today(current)
        payloads = []
        recent_review_day = (current.date() - timedelta(days=1)).isoformat()
        review_payloads = (
            self.review_history.pending(
                auth["user_id"], group_id, since_day=recent_review_day
            )
            if group else []
        )
        review_acks = []
        current_deck_name = (
            self.tracker.current_deck_name
            if self.online.get("share_deck_name", False) and self.tracker.status == "studying"
            else None
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
                and (deck_publish_age < 0 or deck_publish_age >= 60)
            )
        )
        member_cache_key = (auth["user_id"], group_id, study_day)
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
                and previous_route.get("study_day") != study_day
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
                day=study_day,
                active_seconds=int(record["seconds"]),
                answer_count=int(record["answers"]),
                time_goal_minutes=self.tracker.time_goal_minutes,
                card_goal=self.tracker.card_goal,
                status=self.tracker.status,
            )
            payload = {
                "user_id": auth["user_id"],
                "group_id": group["id"],
                "device_id": self.device_id,
                "study_day": study_day,
                "ledger_id": ledger_id,
                **device_snapshot,
            }
            self.sync_outbox.enqueue(payload)
            self.sync_outbox.bind_route(
                user_id=auth["user_id"], group_id=group_id,
                device_id=self.device_id, study_day=study_day, ledger_id=ledger_id,
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
                if update_name:
                    self.client.upsert_profile(token, auth["user_id"], display_name)
                acknowledgements = []
                first_error = None
                ordered_review_payloads = sorted(
                    review_payloads,
                    key=lambda batch: (
                        batch.get("target_day") != study_day,
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
                        if "review day archived" in str(error).casefold():
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
                if fetch_members:
                    try:
                        members = self.client.fetch_group_today(token, group_id, study_day)
                    except SupabaseError as error:
                        if error.status == 401:
                            raise
                        if first_error is None:
                            first_error = error
                    except Exception as error:
                        # Upload acknowledgements remain valid even when the
                        # independent member-list read fails afterwards.
                        if first_error is None:
                            first_error = error
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
                for batch in review_acks:
                    self.review_history.acknowledge(auth["user_id"], group_id, batch)
                self.review_history.compact(
                    (current.date() - timedelta(days=2)).isoformat()
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
                schedule_retry()
                self.save()
            except Exception as error:
                self.online["last_error"] = str(error)
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
