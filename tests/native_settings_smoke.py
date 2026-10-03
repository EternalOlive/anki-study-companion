"""Exercise the native settings dialog with Anki's bundled PyQt6.

This deliberately stays outside the regular unittest suite.  It loads the
real Qt widgets in offscreen mode, supplies a deterministic fake controller,
and never contacts Supabase.  Besides behavioral assertions it writes four
screenshots so the compact dialog can be inspected in both supported
languages and light/dark palettes.
"""

from __future__ import annotations

import os
import re
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ANKI_PACKAGES = Path(
    os.environ.get(
        "ANKI_APP_PACKAGES",
        r"C:\Users\yoon\AppData\Local\Programs\Anki\app_packages",
    )
)
OUTPUT = ROOT / "artifacts"


def _load_settings_types():
    if not ANKI_PACKAGES.exists():
        raise SystemExit(
            "Anki app_packages not found. Set ANKI_APP_PACKAGES to its path."
        )

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(ANKI_PACKAGES))

    from PyQt6 import QtCore, QtGui, QtWidgets

    aqt = types.ModuleType("aqt")
    aqt.mw = None
    qt = types.ModuleType("aqt.qt")
    for module in (QtCore, QtGui, QtWidgets):
        for name in dir(module):
            if name.startswith("Q"):
                setattr(qt, name, getattr(module, name))
    qt.Qt = QtCore.Qt
    sys.modules["aqt"] = aqt
    sys.modules["aqt.qt"] = qt

    package = types.ModuleType("study_companion")
    package.__path__ = [str(ROOT / "study_companion")]
    sys.modules["study_companion"] = package

    from study_companion.online import SupabaseError
    from study_companion.settings import SettingsDialog

    return QtGui, QtWidgets, SettingsDialog, SupabaseError


class FakeTracker:
    def __init__(self, minutes=60, answers=100):
        self.time_goal_minutes = minutes
        self.card_goal = answers


class FakeClient:
    def __init__(self):
        self.calls = []

    def create_group(self, token, name):
        self.calls.append(("create_group", token, name))
        return {"id": "room-created", "name": name, "invite_code": "C7K9"}

    def join_group(self, token, code):
        self.calls.append(("join_group", token, code))
        return "room-joined"

    def leave_group(self, token, group_id):
        self.calls.append(("leave_group", token, group_id))

    def list_groups(self, token, user_id):
        self.calls.append(("list_groups", token, user_id))
        return [
            {
                "id": "room-joined",
                "name": "Quiet room",
                "invite_code": "TMQV",
            }
        ]

    def update_user(self, token, **changes):
        self.calls.append(("update_user", token, changes))
        return {}

    def get_user(self, token):
        self.calls.append(("get_user", token))
        return {"id": "user-local", "is_anonymous": False}

    def sign_in(self, email, password):
        self.calls.append(("sign_in", email, password))
        return {
            "access_token": "signed-in-token",
            "refresh_token": "refresh",
            "user": {"id": "signed-in-user"},
        }

    def upsert_profile(self, token, user_id, name):
        self.calls.append(("upsert_profile", token, user_id, name))


class FakeController:
    """Controller double whose remote operations complete only on request."""

    def __init__(
        self,
        locale="ko",
        *,
        account_kind="guest",
        token=True,
        minutes=60,
        answers=100,
    ):
        self.locale = locale
        self.tracker = FakeTracker(minutes, answers)
        self.closed = False
        self.client = FakeClient()
        self.online = {
            "account_kind": account_kind,
            "display_name": "XTU-3FU",
            "auth": {
                "access_token": "token" if token else "",
                "user_id": "user-local",
            },
        }
        if account_kind == "email":
            self.online["email"] = "reader@example.com"
        self.saved = 0
        self.refreshed = 0
        self.synced = []
        self.identity_attempts = 0
        self.cancelled_identity = 0
        self.pending = None

    def _access_token(self):
        return (self.online.get("auth") or {}).get("access_token")

    def save(self):
        self.saved += 1

    def refresh(self):
        self.refreshed += 1

    def sync_async(self, force=False):
        self.synced.append(force)

    def ensure_online_identity(self):
        self.identity_attempts += 1

    def _cancel_identity_bootstrap(self):
        self.cancelled_identity += 1

    def _store_session(self, result, email, display_name):
        user = result.get("user") or {}
        self.online.update(
            {
                "email": email,
                "display_name": display_name,
                "auth": {
                    "access_token": result["access_token"],
                    "refresh_token": result.get("refresh_token", ""),
                    "user_id": user.get("id") or result.get("user_id"),
                },
            }
        )

    def _queue(self, kind, controls, operation, success, on_error):
        assert self.pending is None, "only one fake remote action may be pending"
        for control in controls:
            control.setEnabled(False)
        self.pending = {
            "kind": kind,
            "controls": controls,
            "operation": operation,
            "success": success,
            "on_error": on_error,
        }

    def _run_authenticated_action(
        self, controls, operation, success, _error_prefix, *, on_error=None
    ):
        self._queue("authenticated", controls, operation, success, on_error)

    def _run_online_action(
        self, controls, task, success, _error_prefix, *, on_error=None
    ):
        self._queue("online", controls, task, success, on_error)

    def finish_remote(self, error=None):
        pending, self.pending = self.pending, None
        assert pending is not None, "no fake remote action is pending"
        for control in pending["controls"]:
            control.setEnabled(True)
        if error is not None:
            pending["on_error"](error)
            return
        if pending["kind"] == "authenticated":
            result = pending["operation"](self._access_token())
        else:
            result = pending["operation"]()
        pending["success"](result)


def _visible_text(dialog, QtWidgets):
    return "\n".join(
        widget.text()
        for widget in dialog.pages.currentWidget().findChildren(QtWidgets.QLabel)
        if widget.isVisible() and widget.text()
    )


def _button(dialog, QtWidgets, text):
    return next(
        button
        for button in dialog.pages.currentWidget().findChildren(QtWidgets.QPushButton)
        if button.text() == text and button.isVisible()
    )


def _fresh_dialog(SettingsDialog, controller):
    dialog = SettingsDialog(controller)
    dialog.resize(470, 560)
    dialog.show()
    return dialog


def check_home_draft_and_save(app, QtWidgets, SettingsDialog):
    controller = FakeController(locale="en", minutes=300, answers=800)
    dialog = _fresh_dialog(SettingsDialog, controller)
    app.processEvents()

    assert dialog.time_goal.value() == 300
    assert dialog.answer_goal.value() == 800
    assert dialog.language.currentData() == "en"
    assert not dialog.save_button.isEnabled()

    dialog.time_goal.setValue(320)
    dialog.answer_goal.setValue(850)
    dialog.language.setCurrentIndex(dialog.language.findData("ko"))
    dialog.show_account()
    dialog.show_home()
    assert dialog.time_goal.value() == 320
    assert dialog.answer_goal.value() == 850
    assert dialog.language.currentData() == "ko"
    assert controller.locale == "en"

    home_text = _visible_text(dialog, QtWidgets)
    assert "오늘" not in home_text and "Today" not in home_text
    assert "멤버" not in home_text and "Members" not in home_text
    assert not re.search(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
        home_text,
        re.IGNORECASE,
    )

    dialog.save_settings()
    assert controller.tracker.time_goal_minutes == 320
    assert controller.tracker.card_goal == 850
    assert controller.locale == "ko"
    assert controller.saved == 1 and controller.refreshed == 1
    assert controller.synced == [True]
    dialog.close()

    cancel_controller = FakeController()
    cancel_dialog = _fresh_dialog(SettingsDialog, cancel_controller)
    cancel_dialog.time_goal.setValue(15)
    cancel_dialog.language.setCurrentIndex(cancel_dialog.language.findData("en"))
    cancel_dialog.reject()
    assert cancel_controller.tracker.time_goal_minutes == 60
    assert cancel_controller.locale == "ko"
    assert cancel_controller.saved == 0


def check_room_flows(app, QtWidgets, SettingsDialog, SupabaseError):
    controller = FakeController()
    dialog = _fresh_dialog(SettingsDialog, controller)
    app.processEvents()

    _button(dialog, QtWidgets, "새 방 만들기").click()
    app.processEvents()
    assert dialog.pages.currentIndex() == dialog.PAGE_CREATE
    assert dialog.group_name.isVisible()
    assert not dialog.invite_code.isVisible()
    dialog.show_home()

    _button(dialog, QtWidgets, "초대 코드로 참여").click()
    app.processEvents()
    assert dialog.pages.currentIndex() == dialog.PAGE_JOIN
    assert dialog.invite_code.isVisible()
    assert not dialog.group_name.isVisible()

    dialog.invite_code.setText("O1l0")
    dialog.join_group()
    assert dialog.join_error.isVisible()
    assert controller.pending is None

    dialog.invite_code.setText(" t m q v ")
    dialog.join_group()
    assert dialog.invite_code.text() == "TMQV"
    assert controller.pending is not None
    assert not dialog.join_submit.isEnabled()
    controller.finish_remote(SupabaseError("temporary failure"))
    app.processEvents()
    assert dialog.join_submit.isEnabled()
    assert dialog.join_error.isVisible()
    assert "temporary failure" in dialog.join_error.text()

    dialog.join_group()
    assert not dialog.join_submit.isEnabled()
    controller.finish_remote()
    app.processEvents()
    assert controller.online["group"]["id"] == "room-joined"
    assert dialog.pages.currentIndex() == dialog.PAGE_HOME
    assert controller.client.calls[0] == ("join_group", "token", "TMQV")

    _button(dialog, QtWidgets, "방 나가기").click()
    app.processEvents()
    assert dialog.pages.currentIndex() == dialog.PAGE_LEAVE
    assert "Quiet room" in _visible_text(dialog, QtWidgets)
    assert "개인 공부 기록은 그대로" in _visible_text(dialog, QtWidgets)
    dialog.leave_group()
    assert controller.pending is not None
    assert not dialog.leave_back.isEnabled()
    assert not dialog.leave_submit.isEnabled()
    controller.finish_remote(SupabaseError("temporary failure"))
    app.processEvents()
    assert dialog.leave_back.isEnabled()
    assert dialog.leave_submit.isEnabled()
    assert "temporary failure" in dialog.leave_error.text()
    assert controller.online["group"]["id"] == "room-joined"

    dialog.leave_group()
    controller.finish_remote()
    app.processEvents()
    assert "group" not in controller.online
    assert dialog.pages.currentIndex() == dialog.PAGE_HOME
    assert controller.client.calls[-1] == ("leave_group", "token", "room-joined")
    assert _button(dialog, QtWidgets, "초대 코드로 참여").isVisible()
    dialog.close()


def check_account_states(app, SettingsDialog):
    guest = FakeController(account_kind="guest", token=True)
    guest_dialog = _fresh_dialog(SettingsDialog, guest)
    guest_dialog.show_account()
    app.processEvents()
    assert "이메일 미연결" in guest_dialog.account_summary.text()
    assert guest_dialog.link_email_button.isVisible()
    assert guest_dialog.login_button.isVisible()
    guest_dialog.show_login()
    app.processEvents()
    assert guest_dialog.login_acknowledge.isVisible()
    assert guest_dialog.login_email.isEnabled()
    assert not guest_dialog.login_submit.isEnabled()
    guest_dialog.login_acknowledge.setChecked(True)
    assert guest_dialog.login_submit.isEnabled()
    guest_dialog.close()

    email = FakeController(account_kind="email", token=True)
    email_dialog = _fresh_dialog(SettingsDialog, email)
    email_dialog.show_account()
    app.processEvents()
    assert "reader@example.com" in email_dialog.account_summary.text()
    assert not email_dialog.link_email_button.isVisible()
    assert not email_dialog.login_button.isVisible()
    email_dialog.close()

    expired = FakeController(account_kind="guest", token=False)
    expired_dialog = _fresh_dialog(SettingsDialog, expired)
    expired_dialog.show_account()
    app.processEvents()
    assert "만료" in expired_dialog.account_summary.text()
    assert not expired_dialog.link_email_button.isVisible()
    assert expired_dialog.login_button.isVisible()
    expired_dialog.show_home()
    app.processEvents()
    retry = next(
        button
        for button in expired_dialog.home_page.findChildren(type(expired_dialog.account_button))
        if button.text() == "다시 시도"
    )
    retry.click()
    assert expired.identity_attempts == 1
    expired_dialog.close()


def _set_palette(app, QtGui, QtWidgets, dark):
    if not dark:
        app.setPalette(QtWidgets.QApplication.style().standardPalette())
        return
    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(43, 43, 43))
    palette.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor(235, 235, 235))
    palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor(35, 35, 35))
    palette.setColor(QtGui.QPalette.ColorRole.AlternateBase, QtGui.QColor(48, 48, 48))
    palette.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor(235, 235, 235))
    palette.setColor(QtGui.QPalette.ColorRole.Button, QtGui.QColor(54, 54, 54))
    palette.setColor(QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor(235, 235, 235))
    palette.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor(74, 118, 168))
    palette.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor(255, 255, 255))
    app.setPalette(palette)


def render_screenshots(app, QtGui, QtWidgets, SettingsDialog):
    OUTPUT.mkdir(exist_ok=True)
    artifacts = []
    for locale in ("ko", "en"):
        for theme in ("light", "dark"):
            _set_palette(app, QtGui, QtWidgets, theme == "dark")
            controller = FakeController(locale=locale)
            controller.online["group"] = {
                "id": "room-1",
                "name": "goyori",
                "invite_code": "35FU",
            }
            dialog = _fresh_dialog(SettingsDialog, controller)
            dialog.resize(470, 560)
            app.processEvents()
            assert 440 <= dialog.width() <= 480
            target = OUTPUT / f"native-settings-{locale}-{theme}.png"
            if not dialog.grab().save(str(target)):
                raise RuntimeError(f"could not save {target}")
            artifacts.append(target)
            if theme == "light":
                dialog.show_leave()
                app.processEvents()
                leave_target = OUTPUT / f"native-settings-leave-{locale}.png"
                if not dialog.grab().save(str(leave_target)):
                    raise RuntimeError(f"could not save {leave_target}")
                artifacts.append(leave_target)
            dialog.close()
    _set_palette(app, QtGui, QtWidgets, False)
    return artifacts


def check_email_confirmation(SettingsDialog, SupabaseError):
    for user, allowed in (
        ({"is_anonymous": True}, False),
        ({"is_anonymous": False, "email": "reader@example.com"}, False),
        ({"is_anonymous": False, "email": "other@example.com", "email_confirmed_at": "2026-10-03"}, False),
        ({"is_anonymous": False, "email": "reader@example.com", "email_confirmed_at": "2026-10-03"}, True),
    ):
        controller = FakeController()
        controller.online["pending_email"] = "reader@example.com"
        controller.client.get_user = lambda token, value=user: value
        dialog = _fresh_dialog(SettingsDialog, controller)
        dialog.new_password.setText("test-password")
        dialog.finish_email_link()
        try:
            controller.finish_remote()
            assert allowed, "unconfirmed/wrong email was linked"
            assert controller.online["account_kind"] == "email"
        except SupabaseError:
            assert not allowed
            assert not any(call[0] == "update_user" for call in controller.client.calls)
        dialog.close()


def main():
    QtGui, QtWidgets, SettingsDialog, SupabaseError = _load_settings_types()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.setStyle("Fusion")
    font_path = os.environ.get("SETTINGS_SMOKE_FONT", r"C:\Windows\Fonts\malgun.ttf")
    font_id = QtGui.QFontDatabase.addApplicationFont(font_path)
    if font_id < 0:
        raise SystemExit(
            "Smoke font could not be loaded. Set SETTINGS_SMOKE_FONT to a TTF path."
        )
    family = QtGui.QFontDatabase.applicationFontFamilies(font_id)[0]
    app.setFont(QtGui.QFont(family, 9))

    check_home_draft_and_save(app, QtWidgets, SettingsDialog)
    check_room_flows(app, QtWidgets, SettingsDialog, SupabaseError)
    check_account_states(app, SettingsDialog)
    check_email_confirmation(SettingsDialog, SupabaseError)
    screenshots = render_screenshots(app, QtGui, QtWidgets, SettingsDialog)
    print("native settings smoke ok")
    for screenshot in screenshots:
        print(f"settings screenshot: {screenshot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
