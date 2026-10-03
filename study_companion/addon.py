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
    QObject,
    QTimer,
    Qt,
)
from aqt.utils import showWarning

from .online import SupabaseClient, SupabaseError
from .nicknames import canonical_nickname, localize_nickname, disambiguate_nickname
from .panel import StudyPanel
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
                QEvent.Type.MouseMove,
                QEvent.Type.Wheel,
            ) and mw.isActiveWindow():
                self.controller.input()
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
        self.locale = data.get("language", "ko" if lang.current_lang.startswith("ko") else "en")
        if self.locale not in ("ko", "en"):
            self.locale = "ko"
        self.ui_state = data.get("ui_state", {})
        self.client = SupabaseClient()
        self.sync_in_flight = False
        self.sync_pending = False
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
        mw.form.menuTools.removeAction(self.action)
        mw.form.menuTools.removeAction(self.panel_action)
        mw.removeDockWidget(self.panel)
        self.panel.deleteLater()

    def save(self):
        data = self.tracker.snapshot()
        data["online"] = self.online
        data["language"] = self.locale
        data["ui_state"] = self.ui_state
        payload = json.dumps(data, ensure_ascii=False)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(self.path)

    def tick(self):
        self.tracker.tick(now())
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
        self.save()
        self.refresh()

    def state_changed(self, new_state, old_state):
        if old_state == "review" and new_state != "review":
            self.tracker.leave_review(now())
            self.save()
            self.refresh()
            self.sync_async(force=True)

    def refresh(self):
        record = self.tracker.today(now())
        minutes, seconds = divmod(int(record["seconds"]), 60)
        status = {"studying": self.t("공부 중", "Studying"), "paused": self.t("잠시 멈춤", "Paused"), "stopped": self.t("접속 중", "Online")}[
            self.tracker.status
        ]
        time_goal = f"/{self.tracker.time_goal_minutes}{self.t('분', 'm')}" if self.tracker.time_goal_minutes else ""
        card_goal = f"/{self.tracker.card_goal}" if self.tracker.card_goal else ""
        self.label.setText(
            f"{self.t('오늘', 'Today')} {minutes:02d}:{seconds:02d}{time_goal}  "
            f"{self.t('답변', 'Answers')} {record['answers']}{card_goal}  {status}"
        )
        self.refresh_panel()

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
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.panel_body = StudyPanel(self)
        self.panel.setWidget(self.panel_body)
        mw.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.panel)
        self.panel_action = self.panel.toggleViewAction()
        mw.form.menuTools.addAction(self.panel_action)
        self.panel.show()

    def refresh_panel(self):
        self.action.setText(self.t("스터디 관리", "Study settings"))
        self.panel.setWindowTitle(self.t("스터디", "Study"))
        self.panel_action.setText(self.t("스터디 패널", "Study panel"))
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
        if self.identity_in_flight or self.online.get("email"):
            return
        if self.online.get("guest_id"):
            self.online["last_error"] = (
                "기존 익명 계정의 접속 정보가 만료되었습니다. 연결한 이메일 계정이 있다면 "
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

    def sync_async(self, force=False):
        if self.closed:
            return
        if self.sync_in_flight:
            if force:
                self.sync_pending = True
            return
        if not self._access_token():
            return
        self.sync_in_flight = True
        auth = dict(self.online["auth"])
        display_name = canonical_nickname(auth["user_id"])
        update_name = self.online.get("display_name") != display_name
        group = dict(self.online.get("group") or {})
        record = self.tracker.today(now())
        payload = None
        if group:
            payload = {
                "group_id": group["id"],
                "user_id": auth["user_id"],
                "study_day": now().date().isoformat(),
                "active_seconds": int(record["seconds"]),
                "answer_count": record["answers"],
                "time_goal_minutes": self.tracker.time_goal_minutes,
                "card_goal": self.tracker.card_goal,
                "status": self.tracker.status,
                "updated_at": now().isoformat(),
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
                if payload is None:
                    return []
                self.client.upsert_daily_stats(token, [payload])
                return self.client.fetch_group_today(token, group["id"], payload["study_day"])
            try:
                members = upload(token)
            except SupabaseError as error:
                if error.status != 401 or not auth.get("refresh_token"):
                    return auth, None, error
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
                    members = upload(token)
                except SupabaseError as refresh_error:
                    if refresh_error.status in (400, 401):
                        refresh_error = SupabaseError("로그인이 만료되었습니다.", status=401)
                    return auth, None, refresh_error
            except Exception as error:
                return auth, None, error
            return auth, members, None

        def done(future):
            self.sync_in_flight = False
            if self.closed or self.online.get("auth", {}).get("user_id") != auth["user_id"]:
                return
            try:
                updated_auth, members, sync_error = future.result()
                self.online["auth"] = updated_auth
                if sync_error is not None:
                    raise sync_error
                self.online["display_name"] = display_name
                self.online["members"] = members
                self.online.pop("last_error", None)
                self.save()
            except SupabaseError as error:
                if error.status == 401:
                    self.online.pop("auth", None)
                    self.online.pop("members", None)
                    self.online["last_error"] = "로그인이 만료되었습니다. 다시 로그인해 주세요."
                else:
                    self.online["last_error"] = str(error)
                self.save()
            except Exception as error:
                self.online["last_error"] = str(error)
                self.save()
            if self.sync_pending:
                self.sync_pending = False
                QTimer.singleShot(0, lambda: self.sync_async(force=True))

        mw.taskman.run_in_background(task, done)


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
