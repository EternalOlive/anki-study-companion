"""Native study settings dialog.

The dialog intentionally keeps local settings separate from remote operations:
goals and language are committed only by the home page's Save button, while
room and account actions run explicitly on their own pages.
"""

from __future__ import annotations

from copy import deepcopy

from aqt import mw
from aqt.qt import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTimer,
    Qt,
    QVBoxLayout,
    QWidget,
)

from .nicknames import canonical_nickname
from .online import SupabaseError


def _clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        child_layout = item.layout()
        if widget is not None:
            widget.hide()
            widget.deleteLater()
        elif child_layout is not None:
            _clear_layout(child_layout)


class SettingsDialog(QDialog):
    """A compact, page-based settings dialog for the study companion."""

    PAGE_HOME = 0
    PAGE_CREATE = 1
    PAGE_JOIN = 2
    PAGE_ACCOUNT = 3
    PAGE_EMAIL = 4
    PAGE_LOGIN = 5
    PAGE_LEAVE = 6

    def __init__(self, controller, parent=None):
        super().__init__(parent or mw)
        self.controller = controller
        self._locale_at_open = controller.locale
        self._time_at_open = controller.tracker.time_goal_minutes
        self._answers_at_open = controller.tracker.card_goal
        self._busy = False
        self._alive = True
        self._controller_signature = None

        self.setWindowTitle(self._t("스터디 관리", "Study settings"))
        self.setMinimumWidth(440)
        self.resize(470, self.sizeHint().height())

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(16)
        self.pages = QStackedWidget(self)
        root.addWidget(self.pages)

        self.home_page = self._build_home_page()
        self.create_page = self._build_create_page()
        self.join_page = self._build_join_page()
        self.account_page = self._build_account_page()
        self.email_page = self._build_email_page()
        self.login_page = self._build_login_page()
        self.leave_page = self._build_leave_page()
        for page in (
            self.home_page,
            self.create_page,
            self.join_page,
            self.account_page,
            self.email_page,
            self.login_page,
            self.leave_page,
        ):
            self.pages.addWidget(page)

        self.finished.connect(self._mark_closed)
        self._reset_draft()
        self.refresh_from_controller()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(600)
        self.refresh_timer.timeout.connect(self._poll_controller)
        self.refresh_timer.start()
        self.show_home()

    def _t(self, korean, english):
        return english if self._locale_at_open == "en" else korean

    def _heading(self, text):
        label = QLabel(text, self)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        return label

    def _note(self, text):
        label = QLabel(text, self)
        label.setWordWrap(True)
        return label

    def _error_label(self):
        label = QLabel("", self)
        label.setWordWrap(True)
        label.setVisible(False)
        return label

    def _set_message(self, label, text):
        label.setText(str(text or ""))
        label.setVisible(bool(text))

    def _section(self, parent, title):
        widget = QWidget(parent)
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self._heading(title))
        return widget, layout

    def _build_home_page(self):
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(20)

        room, room_section_layout = self._section(
            page, self._t("스터디방", "Study room")
        )
        self.room_layout = QVBoxLayout()
        self.room_layout.setContentsMargins(0, 0, 0, 0)
        self.room_layout.setSpacing(8)
        room_section_layout.addLayout(self.room_layout)
        layout.addWidget(room)

        goals, goals_layout = self._section(
            page, self._t("일일 목표", "Daily goals")
        )
        goals_form = QFormLayout()
        goals_form.setHorizontalSpacing(16)
        goals_form.setVerticalSpacing(8)
        self.time_goal = QSpinBox(goals)
        self.time_goal.setObjectName("timeGoal")
        self.time_goal.setRange(0, 1440)
        self.time_goal.setSpecialValueText(self._t("설정 안 함", "Not set"))
        self.time_goal.setSuffix(self._t(" 분", " min"))
        self.answer_goal = QSpinBox(goals)
        self.answer_goal.setObjectName("answerGoal")
        self.answer_goal.setRange(0, 10000)
        self.answer_goal.setSpecialValueText(self._t("설정 안 함", "Not set"))
        self.answer_goal.setSuffix(self._t(" 회", " answers"))
        goals_form.addRow(self._t("공부 시간", "Study time"), self.time_goal)
        goals_form.addRow(self._t("답변", "Answers"), self.answer_goal)
        goals_layout.addLayout(goals_form)
        goals_layout.addWidget(
            self._note(
                self._t(
                    "답변은 같은 카드를 반복해서 답한 횟수도 포함합니다.",
                    "Answers include repeated answers to the same card.",
                )
            )
        )
        layout.addWidget(goals)

        environment, environment_layout = self._section(
            page, self._t("환경", "Preferences")
        )
        language_form = QFormLayout()
        self.language = QComboBox(environment)
        self.language.setObjectName("language")
        self.language.addItem("한국어", "ko")
        self.language.addItem("English", "en")
        language_form.addRow(self._t("언어", "Language"), self.language)
        environment_layout.addLayout(language_form)
        layout.addWidget(environment)

        account_row = QHBoxLayout()
        self.display_code_label = QLabel("—", page)
        self.display_code_label.setObjectName("displayCode")
        self.account_button = QPushButton(
            self._t("계정 관리", "Account"), page
        )
        self.account_button.clicked.connect(self.show_account)
        account_row.addWidget(QLabel(self._t("내 코드", "My code"), page))
        account_row.addWidget(self.display_code_label)
        account_row.addStretch(1)
        account_row.addWidget(self.account_button)
        layout.addLayout(account_row)

        self.home_error = self._error_label()
        layout.addWidget(self.home_error)
        layout.addStretch(1)

        self.home_buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel,
            parent=page,
        )
        self.save_button = self.home_buttons.button(
            QDialogButtonBox.StandardButton.Save
        )
        self.cancel_button = self.home_buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        )
        self.save_button.setText(self._t("저장", "Save"))
        self.cancel_button.setText(self._t("취소", "Cancel"))
        self.home_buttons.accepted.connect(self.save_settings)
        self.home_buttons.rejected.connect(self.reject)
        layout.addWidget(self.home_buttons)

        self.time_goal.valueChanged.connect(self._update_save_enabled)
        self.answer_goal.valueChanged.connect(self._update_save_enabled)
        self.language.currentIndexChanged.connect(self._update_save_enabled)
        return page

    def _detail_page(self, title):
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._heading(title))
        return page, layout

    def _back_row(self, page, primary_text, primary_slot):
        row = QHBoxLayout()
        back = QPushButton(self._t("뒤로", "Back"), page)
        primary = QPushButton(primary_text, page)
        back.clicked.connect(self.show_home)
        primary.clicked.connect(primary_slot)
        row.addStretch(1)
        row.addWidget(back)
        row.addWidget(primary)
        return row, back, primary

    def _build_create_page(self):
        page, layout = self._detail_page(self._t("방 만들기", "Create a room"))
        form = QFormLayout()
        self.group_name = QLineEdit(page)
        self.group_name.setObjectName("groupName")
        self.group_name.setMaxLength(80)
        form.addRow(self._t("방 이름", "Room name"), self.group_name)
        layout.addLayout(form)
        self.create_error = self._error_label()
        layout.addWidget(self.create_error)
        layout.addStretch(1)
        row, self.create_back, self.create_submit = self._back_row(
            page, self._t("만들기", "Create"), self.create_group
        )
        layout.addLayout(row)
        return page

    def _build_join_page(self):
        page, layout = self._detail_page(self._t("방 참여", "Join a room"))
        form = QFormLayout()
        self.invite_code = QLineEdit(page)
        self.invite_code.setObjectName("inviteCode")
        self.invite_code.setMaxLength(32)
        self.invite_code.setPlaceholderText(self._t("예: TMQV", "Example: TMQV"))
        form.addRow(self._t("초대 코드", "Invite code"), self.invite_code)
        layout.addLayout(form)
        self.join_error = self._error_label()
        layout.addWidget(self.join_error)
        layout.addStretch(1)
        row, self.join_back, self.join_submit = self._back_row(
            page, self._t("참여", "Join"), self.join_group
        )
        layout.addLayout(row)
        return page

    def _build_leave_page(self):
        page, layout = self._detail_page(self._t("방 나가기", "Leave room"))
        self.leave_room_name = QLabel("—", page)
        self.leave_room_name.setTextFormat(Qt.TextFormat.PlainText)
        self.leave_room_name.setWordWrap(True)
        font = self.leave_room_name.font()
        font.setBold(True)
        self.leave_room_name.setFont(font)
        layout.addWidget(self.leave_room_name)
        layout.addWidget(
            self._note(
                self._t(
                    "이 방에 공유된 내 오늘 기록은 삭제됩니다. Anki에 저장된 개인 공부 기록은 그대로 남습니다.",
                    "Your shared record for today will be removed from this room. Your personal study record in Anki will remain.",
                )
            )
        )
        self.leave_error = self._error_label()
        layout.addWidget(self.leave_error)
        layout.addStretch(1)
        row, self.leave_back, self.leave_submit = self._back_row(
            page, self._t("방 나가기", "Leave room"), self.leave_group
        )
        layout.addLayout(row)
        return page

    def _build_account_page(self):
        page, layout = self._detail_page(self._t("계정 관리", "Account"))
        self.account_summary = self._note("")
        self.account_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.account_code = QLabel("—", page)
        layout.addWidget(self.account_summary)
        code_row = QHBoxLayout()
        code_row.addWidget(QLabel(self._t("내 코드", "My code"), page))
        code_row.addWidget(self.account_code)
        code_row.addStretch(1)
        layout.addLayout(code_row)
        self.account_error = self._error_label()
        layout.addWidget(self.account_error)
        self.link_email_button = QPushButton(
            self._t("이메일 연결", "Link email"), page
        )
        self.login_button = QPushButton(
            self._t("기존 계정 로그인", "Sign in to an existing account"), page
        )
        self.link_email_button.clicked.connect(self.show_email)
        self.login_button.clicked.connect(self.show_login)
        layout.addWidget(self.link_email_button)
        layout.addWidget(self.login_button)
        layout.addStretch(1)
        back = QPushButton(self._t("뒤로", "Back"), page)
        back.clicked.connect(self.show_home)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(back)
        layout.addLayout(bottom)
        return page

    def _build_email_page(self):
        page, layout = self._detail_page(
            self._t("이메일 연결", "Link an email")
        )
        layout.addWidget(
            self._note(
                self._t(
                    "이 PC의 기록은 유지됩니다. 다른 PC에서 같은 계정을 쓰려면 이메일을 연결하세요.",
                    "Your records on this PC are kept. Link an email to use this account on another PC.",
                )
            )
        )
        form = QFormLayout()
        self.email_address = QLineEdit(page)
        self.email_address.setObjectName("emailAddress")
        form.addRow(self._t("이메일", "Email"), self.email_address)
        layout.addLayout(form)
        self.send_email_button = QPushButton(
            self._t("확인 메일 보내기", "Send verification email"), page
        )
        self.send_email_button.clicked.connect(self.send_verification_email)
        layout.addWidget(self.send_email_button)

        self.password_area = QWidget(page)
        password_layout = QVBoxLayout(self.password_area)
        password_layout.setContentsMargins(0, 8, 0, 0)
        password_layout.setSpacing(8)
        password_layout.addWidget(
            self._note(
                self._t(
                    "메일의 확인 링크를 연 뒤 비밀번호를 설정하세요.",
                    "Open the verification link in your email, then set a password.",
                )
            )
        )
        password_form = QFormLayout()
        self.new_password = QLineEdit(self.password_area)
        self.new_password.setObjectName("newPassword")
        self.new_password.setEchoMode(QLineEdit.EchoMode.Password)
        password_form.addRow(self._t("비밀번호", "Password"), self.new_password)
        password_layout.addLayout(password_form)
        self.finish_email_button = QPushButton(
            self._t("연결 완료", "Finish linking"), self.password_area
        )
        self.finish_email_button.clicked.connect(self.finish_email_link)
        password_layout.addWidget(self.finish_email_button)
        layout.addWidget(self.password_area)
        self.email_error = self._error_label()
        layout.addWidget(self.email_error)
        layout.addStretch(1)
        email_back = QPushButton(self._t("뒤로", "Back"), page)
        email_back.clicked.connect(self.show_account)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(email_back)
        layout.addLayout(bottom)
        return page

    def _build_login_page(self):
        page, layout = self._detail_page(
            self._t("기존 계정 로그인", "Sign in")
        )
        self.login_notice = self._note("")
        layout.addWidget(self.login_notice)
        self.login_acknowledge = QCheckBox(
            self._t(
                "현재 익명 기록과 자동으로 합쳐지지 않음을 확인했습니다.",
                "I understand the current anonymous records will not be merged automatically.",
            ),
            page,
        )
        self.login_acknowledge.toggled.connect(self._update_login_enabled)
        layout.addWidget(self.login_acknowledge)
        form = QFormLayout()
        self.login_email = QLineEdit(page)
        self.login_email.setObjectName("loginEmail")
        self.login_password = QLineEdit(page)
        self.login_password.setObjectName("loginPassword")
        self.login_password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow(self._t("이메일", "Email"), self.login_email)
        form.addRow(self._t("비밀번호", "Password"), self.login_password)
        layout.addLayout(form)
        self.login_error = self._error_label()
        layout.addWidget(self.login_error)
        layout.addStretch(1)
        row, self.login_back, self.login_submit = self._back_row(
            page, self._t("로그인", "Sign in"), self.sign_in
        )
        self.login_back.clicked.disconnect()
        self.login_back.clicked.connect(self.show_account)
        layout.addLayout(row)
        return page

    def _display_code(self):
        code = self.controller.online.get("display_name")
        if code:
            return str(code)
        user_id = (self.controller.online.get("auth") or {}).get("user_id")
        if user_id:
            return canonical_nickname(user_id)
        return "—"

    def refresh_from_controller(self):
        signature = self._current_controller_signature()
        self.display_code_label.setText(self._display_code())
        self.account_code.setText(self._display_code())
        if signature != self._controller_signature:
            self._refresh_room_section()
            self._refresh_account_page()
        pending = self.controller.online.get("pending_email", "")
        if pending and not self.email_address.text():
            self.email_address.setText(pending)
        self.password_area.setVisible(bool(pending))
        self._controller_signature = signature

    def _current_controller_signature(self):
        online = self.controller.online
        group = online.get("group") or {}
        return (
            bool(self.controller._access_token()),
            group.get("id"),
            group.get("name"),
            group.get("invite_code"),
            online.get("display_name"),
            online.get("account_kind"),
            online.get("email"),
            online.get("pending_email"),
            online.get("last_error"),
        )

    def _poll_controller(self):
        if not self._valid():
            self.refresh_timer.stop()
            return
        signature = self._current_controller_signature()
        if signature == self._controller_signature:
            return
        # Refresh only server-backed portions. Goal and language drafts remain intact.
        self.display_code_label.setText(self._display_code())
        self.account_code.setText(self._display_code())
        self._refresh_room_section()
        self._refresh_account_page()
        self.password_area.setVisible(bool(self.controller.online.get("pending_email")))
        self._controller_signature = signature

    def _refresh_room_section(self):
        _clear_layout(self.room_layout)
        group = self.controller.online.get("group")
        if group:
            name = QLabel(
                str(group.get("name") or self._t("친구 그룹", "Study room")), self
            )
            name.setTextFormat(Qt.TextFormat.PlainText)
            font = name.font()
            font.setBold(True)
            name.setFont(font)
            name.setWordWrap(True)
            self.room_layout.addWidget(name)
            row = QHBoxLayout()
            code = str(group.get("invite_code") or "—")
            self.room_invite_code = QLabel(code, self)
            self.copy_invite_button = QPushButton(self._t("복사", "Copy"), self)
            self.copy_feedback = QLabel("", self)
            self.copy_invite_button.setEnabled(code != "—")
            self.copy_invite_button.clicked.connect(self.copy_invite_code)
            row.addWidget(QLabel(self._t("초대 코드", "Invite code"), self))
            row.addWidget(self.room_invite_code)
            row.addWidget(self.copy_invite_button)
            row.addWidget(self.copy_feedback)
            row.addStretch(1)
            self.room_layout.addLayout(row)
            leave_row = QHBoxLayout()
            leave_row.addStretch(1)
            leave = QPushButton(self._t("방 나가기", "Leave room"), self)
            leave.clicked.connect(self.show_leave)
            leave_row.addWidget(leave)
            self.room_layout.addLayout(leave_row)
            return

        if not self.controller._access_token():
            self.room_layout.addWidget(
                self._note(
                    self._t(
                        "익명 계정을 준비하면 방을 만들거나 참여할 수 있습니다.",
                        "An anonymous account is needed to create or join a room.",
                    )
                )
            )
            error = self.controller.online.get("last_error")
            if error:
                self.room_layout.addWidget(self._note(str(error)))
            retry = QPushButton(self._t("다시 시도", "Try again"), self)
            retry.clicked.connect(self.controller.ensure_online_identity)
            self.room_layout.addWidget(retry)
            return

        row = QHBoxLayout()
        join = QPushButton(self._t("초대 코드로 참여", "Join with a code"), self)
        create = QPushButton(self._t("새 방 만들기", "Create a room"), self)
        join.clicked.connect(self.show_join)
        create.clicked.connect(self.show_create)
        row.addWidget(join)
        row.addWidget(create)
        row.addStretch(1)
        self.room_layout.addLayout(row)

    def _refresh_account_page(self):
        kind = self.controller.online.get("account_kind")
        email = self.controller.online.get("email")
        token = self.controller._access_token()
        if kind == "email" and email:
            self.account_summary.setText(
                self._t(f"연결된 이메일\n{email}", f"Linked email\n{email}")
            )
            self.link_email_button.setVisible(False)
            self.login_button.setVisible(False)
        elif token:
            self.account_summary.setText(
                self._t(
                    "이메일 미연결\n현재 기록은 이 PC에 보존됩니다.",
                    "Email not linked\nYour current records are kept on this PC.",
                )
            )
            self.link_email_button.setVisible(True)
            self.login_button.setVisible(True)
        else:
            self.account_summary.setText(
                self._t(
                    "계정 연결이 만료되었거나 아직 준비되지 않았습니다.",
                    "The account session has expired or is not ready yet.",
                )
            )
            self.link_email_button.setVisible(False)
            self.login_button.setVisible(True)

    def show_home(self):
        self.refresh_from_controller()
        self.pages.setCurrentIndex(self.PAGE_HOME)
        self._update_save_enabled()

    def show_create(self):
        self._set_message(self.create_error, "")
        self.pages.setCurrentIndex(self.PAGE_CREATE)
        self.group_name.setFocus()

    def show_join(self):
        self._set_message(self.join_error, "")
        self.pages.setCurrentIndex(self.PAGE_JOIN)
        self.invite_code.setFocus()

    def show_leave(self):
        group = self.controller.online.get("group") or {}
        if not group.get("id"):
            self.show_home()
            return
        self._set_message(self.leave_error, "")
        self.leave_room_name.setText(
            str(group.get("name") or self._t("친구 그룹", "Study room"))
        )
        self.pages.setCurrentIndex(self.PAGE_LEAVE)
        self.leave_submit.setFocus()

    def show_account(self):
        self.refresh_from_controller()
        self.pages.setCurrentIndex(self.PAGE_ACCOUNT)

    def show_email(self):
        self._set_message(self.email_error, "")
        self.refresh_from_controller()
        self.pages.setCurrentIndex(self.PAGE_EMAIL)
        self.email_address.setFocus()

    def show_login(self):
        self._set_message(self.login_error, "")
        active_guest = (
            self.controller.online.get("account_kind") == "guest"
            and bool(self.controller._access_token())
        )
        self.login_notice.setText(
                self._t(
                    "현재 익명 기록은 로그인 계정과 합쳐지지 않습니다. 전환 전 정보는 이 PC에 복구용으로 보관됩니다.",
                    "The current anonymous records will not be merged. Recovery information is kept on this PC before switching.",
            )
            if active_guest
            else self._t(
                "연결했던 이메일 계정으로 로그인합니다.",
                "Sign in to an email account you previously linked.",
            )
        )
        self.login_acknowledge.setVisible(active_guest)
        self.login_acknowledge.setChecked(False)
        self.login_email.setEnabled(True)
        self.login_password.setEnabled(True)
        self._update_login_enabled()
        self.pages.setCurrentIndex(self.PAGE_LOGIN)
        self.login_email.setFocus()

    def _update_login_enabled(self, *_args):
        active_guest = (
            self.controller.online.get("account_kind") == "guest"
            and bool(self.controller._access_token())
        )
        acknowledged = not active_guest or self.login_acknowledge.isChecked()
        self.login_submit.setEnabled(acknowledged and not self._busy)

    def _has_changes(self):
        return (
            self.time_goal.value() != self._time_at_open
            or self.answer_goal.value() != self._answers_at_open
            or self.language.currentData() != self._locale_at_open
        )

    def _update_save_enabled(self, *_args):
        self.save_button.setEnabled(self._has_changes())

    def _reset_draft(self):
        self._time_at_open = self.controller.tracker.time_goal_minutes
        self._answers_at_open = self.controller.tracker.card_goal
        self._locale_at_open = self.controller.locale
        self.time_goal.setValue(self._time_at_open)
        self.answer_goal.setValue(self._answers_at_open)
        index = self.language.findData(self._locale_at_open)
        self.language.setCurrentIndex(max(index, 0))
        self._update_save_enabled()

    def save_settings(self):
        previous_time = self.controller.tracker.time_goal_minutes
        previous_answers = self.controller.tracker.card_goal
        previous_locale = self.controller.locale
        self.controller.tracker.time_goal_minutes = self.time_goal.value()
        self.controller.tracker.card_goal = self.answer_goal.value()
        locale = self.language.currentData()
        self.controller.locale = locale if locale in ("ko", "en") else "ko"
        try:
            self.controller.save()
        except Exception as error:
            self.controller.tracker.time_goal_minutes = previous_time
            self.controller.tracker.card_goal = previous_answers
            self.controller.locale = previous_locale
            self.controller.refresh()
            self._set_message(
                self.home_error,
                self._t(
                    f"설정을 저장하지 못했습니다.\n{error}",
                    f"Could not save settings.\n{error}",
                ),
            )
            return
        self.controller.refresh()
        self.controller.sync_async(force=True)
        self.accept()

    def copy_invite_code(self):
        code = self.room_invite_code.text()
        if not code or code == "—":
            return
        QApplication.clipboard().setText(code)
        self.copy_feedback.setText(self._t("복사됨", "Copied"))
        QTimer.singleShot(1800, self._clear_copy_feedback)

    def _clear_copy_feedback(self):
        if self._valid() and hasattr(self, "copy_feedback"):
            try:
                self.copy_feedback.clear()
            except RuntimeError:
                pass

    def _valid(self):
        return self._alive and not self.controller.closed

    def _mark_closed(self, _result):
        self._alive = False
        if hasattr(self, "refresh_timer"):
            self.refresh_timer.stop()

    def _begin_remote(self, error_label):
        if self._busy:
            self._set_message(
                error_label,
                self._t("다른 작업이 끝날 때까지 기다려 주세요.", "Wait for the current action to finish."),
            )
            return False
        self._busy = True
        self._set_message(error_label, "")
        return True

    def _finish_remote(self):
        self._busy = False

    def _remote_error(self, label, _prefix=None):
        def handle(message):
            self._finish_remote()
            if self._valid():
                self._set_message(label, str(message or ""))

        return handle

    def create_group(self):
        name = self.group_name.text().strip()
        if not name:
            self._set_message(
                self.create_error,
                self._t("방 이름을 입력해 주세요.", "Enter a room name."),
            )
            return
        if not self._begin_remote(self.create_error):
            return

        def success(group):
            self._finish_remote()
            if not group:
                if self._valid():
                    self._set_message(
                        self.create_error,
                        self._t(
                            "생성된 방 정보를 받지 못했습니다.",
                            "The created room was not returned.",
                        ),
                    )
                return
            self.controller.online["group"] = group
            self.controller.save()
            self.controller.sync_async(force=True)
            if self._valid():
                self.show_home()

        self.controller._run_authenticated_action(
            [self.create_submit],
            lambda token: self.controller.client.create_group(token, name),
            success,
            self._t("방을 만들지 못했습니다.", "Could not create the room."),
            on_error=self._remote_error(
                self.create_error,
                self._t("방을 만들지 못했습니다.", "Could not create the room."),
            ),
        )

    def join_group(self):
        code = "".join(self.invite_code.text().split()).upper()
        self.invite_code.setText(code)
        allowed = set("23456789ABCDEFGHJKLMNPQRSTUVWXYZ")
        if len(code) != 4 or any(character not in allowed for character in code):
            self._set_message(
                self.join_error,
                self._t(
                    "초대 코드는 영문 대문자와 숫자 4자리입니다.",
                    "The invite code is 4 uppercase letters or digits.",
                ),
            )
            return
        if not self._begin_remote(self.join_error):
            return
        user_id = (self.controller.online.get("auth") or {}).get("user_id")
        if not user_id:
            self._finish_remote()
            self._set_message(
                self.join_error,
                self._t("계정 준비가 필요합니다.", "The account is not ready yet."),
            )
            return

        def operation(token):
            group_id = self.controller.client.join_group(token, code)
            groups = self.controller.client.list_groups(token, user_id)
            return group_id, groups

        def success(value):
            self._finish_remote()
            group_id, groups = value
            self.controller.online["group"] = next(
                (item for item in groups if item.get("id") == group_id),
                {
                    "id": group_id,
                    "name": self._t("친구 그룹", "Study room"),
                    "invite_code": code,
                },
            )
            self.controller.save()
            self.controller.sync_async(force=True)
            if self._valid():
                self.show_home()

        self.controller._run_authenticated_action(
            [self.join_submit],
            operation,
            success,
            self._t("방에 참여하지 못했습니다.", "Could not join the room."),
            on_error=self._remote_error(
                self.join_error,
                self._t("방에 참여하지 못했습니다.", "Could not join the room."),
            ),
        )

    def leave_group(self):
        group = dict(self.controller.online.get("group") or {})
        group_id = group.get("id")
        if not group_id:
            self.show_home()
            return
        if not self._begin_remote(self.leave_error):
            return

        def success(_result):
            self._finish_remote()
            current = self.controller.online.get("group") or {}
            if current.get("id") == group_id:
                self.controller.online.pop("group", None)
                self.controller.online.pop("members", None)
            self.controller.save()
            self.controller.refresh()
            if self._valid():
                self.show_home()

        self.controller._run_authenticated_action(
            [self.leave_back, self.leave_submit],
            lambda token: self.controller.client.leave_group(token, str(group_id)),
            success,
            self._t("방에서 나가지 못했습니다.", "Could not leave the room."),
            on_error=self._remote_error(
                self.leave_error,
                self._t("방에서 나가지 못했습니다.", "Could not leave the room."),
            ),
        )

    def send_verification_email(self):
        address = self.email_address.text().strip()
        if "@" not in address or address.startswith("@") or address.endswith("@"):
            self._set_message(
                self.email_error,
                self._t("올바른 이메일을 입력해 주세요.", "Enter a valid email address."),
            )
            return
        if not self._begin_remote(self.email_error):
            return

        def success(_result):
            self._finish_remote()
            self.controller.online["pending_email"] = address
            self.controller.save()
            if self._valid():
                self.password_area.setVisible(True)
                self._set_message(
                    self.email_error,
                    self._t(
                        "서버가 인증 메일 요청을 접수했습니다. 받은 메일의 링크를 연 뒤 비밀번호를 설정하세요. 메일이 없으면 스팸함과 발송 설정을 확인해 주세요.",
                        "The server accepted the email request. Open the link in your inbox, then set a password. If it does not arrive, check spam and email delivery settings.",
                    ),
                )
                self.new_password.setFocus()

        self.controller._run_authenticated_action(
            [self.send_email_button],
            lambda token: self.controller.client.update_user(token, email=address),
            success,
            self._t("이메일을 연결하지 못했습니다.", "Could not link the email."),
            on_error=self._remote_error(
                self.email_error,
                self._t("이메일을 연결하지 못했습니다.", "Could not link the email."),
            ),
        )

    def finish_email_link(self):
        password = self.new_password.text()
        pending_address = self.controller.online.get(
            "pending_email", self.email_address.text().strip()
        )
        if len(password) < 6:
            self._set_message(
                self.email_error,
                self._t(
                    "비밀번호는 6자 이상으로 입력해 주세요.",
                    "Use at least 6 characters for the password.",
                ),
            )
            return
        if not self._begin_remote(self.email_error):
            return

        def operation(token):
            user = self.controller.client.get_user(token)
            if (user.get("is_anonymous", True)
                    or not user.get("email_confirmed_at")
                    or (user.get("email") or "").casefold() != pending_address.casefold()):
                raise SupabaseError(
                    self._t(
                        "먼저 이메일의 확인 링크를 열어 주세요.",
                        "Open the verification link in your email first.",
                    )
                )
            self.controller.client.update_user(token, password=password)
            return user

        def success(_user):
            self._finish_remote()
            self.controller.online["account_kind"] = "email"
            self.controller.online["email"] = self.controller.online.pop(
                "pending_email", pending_address
            )
            self.controller.save()
            if self._valid():
                self.new_password.clear()
                self.show_account()

        self.controller._run_authenticated_action(
            [self.finish_email_button],
            operation,
            success,
            self._t(
                "계정 연결을 마치지 못했습니다.",
                "Could not finish linking the account.",
            ),
            on_error=self._remote_error(
                self.email_error,
                self._t(
                    "계정 연결을 마치지 못했습니다.",
                    "Could not finish linking the account.",
                ),
            ),
        )

    def sign_in(self):
        active_guest = (
            self.controller.online.get("account_kind") == "guest"
            and bool(self.controller._access_token())
        )
        if active_guest and not self.login_acknowledge.isChecked():
            self._set_message(
                self.login_error,
                self._t(
                    "계정 전환 안내를 확인해 주세요.",
                    "Confirm the account-switching notice first.",
                ),
            )
            return
        address = self.login_email.text().strip()
        password = self.login_password.text()
        if not address or not password:
            self._set_message(
                self.login_error,
                self._t(
                    "이메일과 비밀번호를 입력해 주세요.",
                    "Enter your email and password.",
                ),
            )
            return
        if not self._begin_remote(self.login_error):
            return
        self.controller._cancel_identity_bootstrap()

        def task():
            result = self.controller.client.sign_in(address, password)
            user = result.get("user") or {}
            user_id = user.get("id") or result.get("user_id")
            if not user_id:
                raise SupabaseError(
                    self._t(
                        "로그인 응답에서 사용자 정보를 찾지 못했습니다.",
                        "The sign-in response did not include a user.",
                    )
                )
            name = canonical_nickname(user_id)
            self.controller.client.upsert_profile(
                result["access_token"], user_id, name
            )
            groups = self.controller.client.list_groups(result["access_token"], user_id)
            return result, groups, name

        def success(value):
            self._finish_remote()
            result, groups, name = value
            if active_guest:
                previous = self.controller.online
                self.controller.online["guest_session_backup"] = {
                    key: deepcopy(previous[key])
                    for key in (
                        "auth",
                        "guest_id",
                        "display_name",
                        "account_kind",
                        "email",
                        "pending_email",
                        "group",
                        "members",
                    )
                    if key in previous
                }
            self.controller.online.pop("group", None)
            self.controller.online.pop("members", None)
            self.controller._store_session(result, address, name)
            self.controller.online["account_kind"] = "email"
            self.controller.online.pop("pending_email", None)
            if groups:
                self.controller.online["group"] = groups[0]
            self.controller.save()
            self.controller.sync_async(force=True)
            if self._valid():
                self.login_password.clear()
                self._reset_draft()
                self.show_home()

        self.controller._run_online_action(
            [self.login_submit],
            task,
            success,
            self._t("로그인하지 못했습니다.", "Could not sign in."),
            on_error=self._remote_error(
                self.login_error,
                self._t("로그인하지 못했습니다.", "Could not sign in."),
            ),
        )
