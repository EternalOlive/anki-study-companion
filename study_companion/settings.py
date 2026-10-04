"""Native study settings dialog.

The dialog intentionally keeps local settings separate from remote operations:
goals and language are committed only by the home page's Save button, while
room and account actions run explicitly on their own pages.
"""

from __future__ import annotations

from copy import deepcopy
import re

from aqt import mw
from aqt.qt import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDateTime,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QTimeZone,
    QTimer,
    QToolButton,
    Qt,
    QVBoxLayout,
    QWidget,
)

from .nicknames import canonical_nickname
from .online import SupabaseError
from .study_day import DAY_START_HOUR, DEFAULT_TIME_ZONE, room_time_zone
from .ux_services import (
    ANSWER_GOAL_MAX,
    TIME_GOAL_MAX_MINUTES,
    normalize_room_name,
    validate_goal,
    validate_invite_code,
)


def _time_zone_text(value):
    """Return a Qt time-zone identifier as plain text."""

    if hasattr(value, "data"):
        value = value.data()
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return str(value or "")


def _available_time_zones():
    zones = sorted({
        name for name in map(_time_zone_text, QTimeZone.availableTimeZoneIds())
        if name
    })
    return zones or [DEFAULT_TIME_ZONE]


def _system_time_zone_name(zones):
    current = _time_zone_text(QTimeZone.systemTimeZoneId())
    return current if current in zones else DEFAULT_TIME_ZONE


def _apply_group_time_zone(controller, group):
    apply = getattr(controller, "apply_room_time_zone", None)
    if callable(apply):
        apply(room_time_zone(group))


def _already_left_error(error):
    """Recognize only the server's explicit idempotent leave condition."""
    return str(error).strip().casefold() == "not a member of this group"


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
    PAGE_EMAIL = 3
    PAGE_LOGIN = 4
    PAGE_LEAVE = 5
    PAGE_RECOVER = 6
    TAB_ROOM = 0
    TAB_SETTINGS = 1
    TAB_ACCOUNT = 2

    def __init__(self, controller, parent=None):
        super().__init__(parent or mw)
        self.controller = controller
        self._locale_at_open = controller.locale
        self._time_at_open = controller.tracker.time_goal_minutes
        self._answers_at_open = controller.tracker.card_goal
        self._share_deck_at_open = bool(
            controller.online.get("share_deck_name", True)
        )
        self._busy = False
        self._alive = True
        self._controller_signature = None
        self._copy_feedback_generation = {}
        self._members_loaded_context = None

        self.setWindowTitle(self._t("스터디 관리", "Study settings"))
        self.setMinimumSize(440, 420)
        self.resize(470, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(16)
        self.pages = QStackedWidget(self)
        root.addWidget(self.pages)

        self.home_page = self._build_home_page()
        self.create_page = self._build_create_page()
        self.join_page = self._build_join_page()
        self.email_page = self._build_email_page()
        self.login_page = self._build_login_page()
        self.leave_page = self._build_leave_page()
        self.recover_page = self._build_recover_page()
        for page in (
            self.home_page,
            self.create_page,
            self.join_page,
            self.email_page,
            self.login_page,
            self.leave_page,
            self.recover_page,
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

    def _scrolling_tab(self, parent):
        scroll = QScrollArea(parent)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget(scroll)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(4, 14, 8, 8)
        layout.setSpacing(20)
        scroll.setWidget(content)
        return scroll, content, layout

    def _build_home_page(self):
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.home_tabs = QTabWidget(page)
        self.home_tabs.setObjectName("settingsTabs")
        self.home_tabs.setDocumentMode(True)
        self.home_tabs.setUsesScrollButtons(False)
        self.room_tab, self.room_tab_content, room_tab_layout = (
            self._scrolling_tab(self.home_tabs)
        )
        self.settings_tab, self.settings_tab_content, settings_tab_layout = (
            self._scrolling_tab(self.home_tabs)
        )
        self.account_tab, self.account_tab_content, account_tab_layout = (
            self._scrolling_tab(self.home_tabs)
        )
        self.home_tabs.addTab(self.room_tab, self._t("방", "Room"))
        self.home_tabs.addTab(self.settings_tab, self._t("내 설정", "My settings"))
        self.home_tabs.addTab(self.account_tab, self._t("계정", "Account"))
        layout.addWidget(self.home_tabs, 1)

        room, room_section_layout = self._section(
            self.room_tab_content, self._t("스터디방", "Study room")
        )
        self.room_layout = QVBoxLayout()
        self.room_layout.setContentsMargins(0, 0, 0, 0)
        self.room_layout.setSpacing(8)
        room_section_layout.addLayout(self.room_layout)
        room_tab_layout.addWidget(room)

        self.members_section, members_section_layout = self._section(
            self.room_tab_content, self._t("멤버", "Members")
        )
        self.members_list_layout = QVBoxLayout()
        self.members_list_layout.setContentsMargins(0, 0, 0, 0)
        self.members_list_layout.setSpacing(8)
        members_section_layout.addLayout(self.members_list_layout)
        self.members_error = self._error_label()
        members_section_layout.addWidget(self.members_error)
        self.members_refresh = QPushButton(
            self._t("새로고침", "Refresh"), self.members_section
        )
        self.members_refresh.clicked.connect(self.load_members)
        members_refresh_row = QHBoxLayout()
        members_refresh_row.addStretch(1)
        members_refresh_row.addWidget(self.members_refresh)
        members_section_layout.addLayout(members_refresh_row)
        room_tab_layout.addWidget(self.members_section)
        room_tab_layout.addStretch(1)

        self.record_status_toggle = QToolButton(self.settings_tab_content)
        self.record_status_toggle.setObjectName("recordStatusToggle")
        self.record_status_toggle.setText(self._t("기록 반영", "Record status"))
        self.record_status_toggle.setCheckable(True)
        self.record_status_toggle.setAutoRaise(True)
        self.record_status_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.record_status_toggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.record_status_toggle.setAccessibleName(
            self._t("기록 반영 상세", "Record status details")
        )
        self.record_status_toggle.toggled.connect(self._toggle_record_status)
        settings_tab_layout.addWidget(self.record_status_toggle)

        self.record_status_details = QWidget(self.settings_tab_content)
        self.record_status_details.setObjectName("recordStatusDetails")
        status_layout = QVBoxLayout(self.record_status_details)
        status_layout.setContentsMargins(18, 0, 0, 0)
        status_layout.setSpacing(6)
        status_form = QFormLayout()
        status_form.setContentsMargins(0, 0, 0, 0)
        status_form.setHorizontalSpacing(16)
        status_form.setVerticalSpacing(5)
        self.record_status_rows = {}
        for key, korean, english in (
            ("local_read_at", "이 PC 기록 확인", "Records read on this PC"),
            ("local_save_at", "이 PC 기록 저장", "Records saved on this PC"),
            ("upload_at", "방에 공유", "Shared with room"),
            ("members_at", "친구 기록 조회", "Friend records refreshed"),
        ):
            title = QLabel(self._t(korean, english), self.record_status_details)
            value = QLabel("—", self.record_status_details)
            value.setAlignment(Qt.AlignmentFlag.AlignRight)
            status_form.addRow(title, value)
            self.record_status_rows[key] = (title, value)
        status_layout.addLayout(status_form)
        self.record_status_errors = QLabel("", self.record_status_details)
        self.record_status_errors.setObjectName("recordStatusErrors")
        self.record_status_errors.setWordWrap(True)
        self.record_status_errors.setVisible(False)
        status_layout.addWidget(self.record_status_errors)
        self.record_status_zone = QLabel("", self.record_status_details)
        self.record_status_zone.setWordWrap(True)
        status_layout.addWidget(self.record_status_zone)
        self.record_status_mobile = self._note(
            self._t(
                "모바일 기록은 모바일과 PC에서 Anki 동기화 후 반영됩니다.",
                "Mobile records appear after syncing Anki on mobile and this PC.",
            )
        )
        status_layout.addWidget(self.record_status_mobile)
        self.record_status_details.setVisible(False)
        settings_tab_layout.addWidget(self.record_status_details)

        sharing, sharing_layout = self._section(
            self.settings_tab_content,
            self._t("방에 공유하는 정보", "Information shared with the room"),
        )
        sharing_layout.addWidget(
            self._note(
                self._t(
                    "방 멤버는 공부 시간·답변 수·오늘 답변 시간대·공부 중 상태를 볼 수 있습니다. "
                    "방의 하루는 방에서 정한 시간대의 04:00에 바뀝니다.",
                    "Room members can see your study time, answer count, today's answer activity, and studying status. "
                    "The room day resets at 04:00 in the room's time zone.",
                )
            )
        )
        self.share_deck_name = QCheckBox(
            self._t("공부 중인 덱 이름 공유", "Share current deck name"), sharing
        )
        self.share_deck_name.setObjectName("shareDeckName")
        sharing_layout.addWidget(self.share_deck_name)
        sharing_layout.addWidget(
            self._note(
                self._t(
                    "기본적으로 같은 방 멤버에게 표시됩니다. 끄면 숨겨지며 카드 내용은 공유하지 않습니다.",
                    "Shown to room members by default. Turn it off to hide it. Card contents are not shared.",
                )
            )
        )
        settings_tab_layout.addWidget(sharing)

        goals, goals_layout = self._section(
            self.settings_tab_content, self._t("일일 목표", "Daily goals")
        )
        goals_form = QFormLayout()
        goals_form.setHorizontalSpacing(16)
        goals_form.setVerticalSpacing(8)
        self.time_goal = QSpinBox(goals)
        self.time_goal.setObjectName("timeGoal")
        self.time_goal.setRange(0, TIME_GOAL_MAX_MINUTES)
        self.time_goal.setSpecialValueText(self._t("설정 안 함", "Not set"))
        self.time_goal.setSuffix(self._t(" 분", " min"))
        self.answer_goal = QSpinBox(goals)
        self.answer_goal.setObjectName("answerGoal")
        self.answer_goal.setRange(0, ANSWER_GOAL_MAX)
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
        settings_tab_layout.addWidget(goals)

        environment, environment_layout = self._section(
            self.settings_tab_content, self._t("환경", "Preferences")
        )
        language_form = QFormLayout()
        self.language = QComboBox(environment)
        self.language.setObjectName("language")
        self.language.addItem("한국어", "ko")
        self.language.addItem("English", "en")
        language_form.addRow(self._t("언어", "Language"), self.language)
        environment_layout.addLayout(language_form)
        settings_tab_layout.addWidget(environment)
        settings_tab_layout.addStretch(1)

        self.account_summary = self._note("")
        self.account_summary.setTextFormat(Qt.TextFormat.PlainText)
        account_tab_layout.addWidget(self.account_summary)
        account_row = QHBoxLayout()
        self.display_code_label = QLabel("—", self.account_tab_content)
        self.display_code_label.setObjectName("displayCode")
        self.account_code = self.display_code_label
        account_row.addWidget(
            QLabel(self._t("내 코드", "My code"), self.account_tab_content)
        )
        account_row.addWidget(self.display_code_label)
        account_row.addStretch(1)
        account_tab_layout.addLayout(account_row)
        self.account_error = self._error_label()
        account_tab_layout.addWidget(self.account_error)
        self.link_email_button = QPushButton(
            self._t("통합 계정 만들기", "Create synced account"),
            self.account_tab_content,
        )
        self.login_button = QPushButton(
            self._t("다른 PC의 계정으로 로그인", "Sign in on this PC"),
            self.account_tab_content,
        )
        self.restart_guest_button = QPushButton(
            self._t("새 익명 계정 시작", "Start a new guest account"),
            self.account_tab_content,
        )
        self.link_email_button.clicked.connect(self.show_email)
        self.login_button.clicked.connect(self.show_login)
        self.restart_guest_button.clicked.connect(self.restart_expired_guest)
        account_tab_layout.addWidget(self.link_email_button)
        account_tab_layout.addWidget(self.login_button)
        account_tab_layout.addWidget(self.restart_guest_button)
        account_tab_layout.addStretch(1)

        self.home_error = self._error_label()
        layout.addWidget(self.home_error)

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
        self.close_button = QPushButton(self._t("닫기", "Close"), page)
        self.close_button.clicked.connect(self.reject)
        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(self.close_button)
        footer.addWidget(self.home_buttons)
        layout.addLayout(footer)

        self.time_goal.valueChanged.connect(self._update_save_enabled)
        self.answer_goal.valueChanged.connect(self._update_save_enabled)
        self.language.currentIndexChanged.connect(self._update_save_enabled)
        self.share_deck_name.toggled.connect(self._update_save_enabled)
        self.home_tabs.currentChanged.connect(self._home_tab_changed)
        self.home_buttons.setVisible(False)
        self.close_button.setVisible(True)
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
        layout.addWidget(self._room_participation_notice(page))
        form = QFormLayout()
        self.group_name = QLineEdit(page)
        self.group_name.setObjectName("groupName")
        self.group_name.setMaxLength(80)
        form.addRow(self._t("방 이름", "Room name"), self.group_name)
        self.room_time_zone = QComboBox(page)
        self.room_time_zone.setObjectName("roomTimeZone")
        self.room_time_zone.setEditable(True)
        self.room_time_zone.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.room_time_zone.setMaxVisibleItems(16)
        time_zones = _available_time_zones()
        self.room_time_zone.addItems(time_zones)
        selected_zone = _system_time_zone_name(time_zones)
        selected_index = self.room_time_zone.findText(selected_zone)
        self.room_time_zone.setCurrentIndex(max(0, selected_index))
        self.room_time_zone.setToolTip(
            self._t(
                "이 방의 기록은 선택한 시간대의 04:00에 새로 시작합니다. 방을 만든 뒤에는 바꿀 수 없습니다.",
                "Room records reset at 04:00 in this time zone. It cannot be changed after creation.",
            )
        )
        form.addRow(self._t("시간대", "Time zone"), self.room_time_zone)
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
        layout.addWidget(self._room_participation_notice(page))
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

    def _room_participation_notice(self, parent):
        label = QLabel(
            self._t(
                "방에 참여하면 공부 시간·답변 수·오늘 답변 시간대·공부 중 상태가 멤버에게 공유됩니다. "
                "선택한 경우에만 덱 이름도 공유하며, 카드 내용은 공유하지 않습니다. "
                "방의 하루는 방에서 정한 시간대의 04:00에 바뀝니다.",
                "Creating or joining a room shares your study time, answer count, today's answer activity, and studying status "
                "with its members. Your deck name is shared only when enabled; card contents are not shared. "
                "The room day resets at 04:00 in the room's time zone.",
            ),
            parent,
        )
        label.setWordWrap(True)
        return label

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

    def _build_email_page(self):
        page, layout = self._detail_page(
            self._t("통합 계정 만들기", "Create synced account")
        )
        layout.addWidget(
            self._note(
                self._t(
                    "현재 기록과 방을 그대로 유지하면서 로그인 아이디를 만듭니다.",
                    "Create a login ID while keeping the current records and room.",
                )
            )
        )
        form = QFormLayout()
        self.email_address = QLineEdit(page)
        self.email_address.setObjectName("accountUsername")
        self.email_address.setMaxLength(24)
        self.email_address.setPlaceholderText(self._t("영문 소문자·숫자·밑줄", "lowercase letters, digits, underscore"))
        form.addRow(self._t("아이디", "Username"), self.email_address)
        self.new_password = QLineEdit(page)
        self.new_password.setObjectName("newPassword")
        self.new_password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow(self._t("비밀번호", "Password"), self.new_password)
        layout.addLayout(form)
        self.finish_email_button = QPushButton(
            self._t("계정 만들기", "Create account"), page
        )
        self.finish_email_button.clicked.connect(self.finish_email_link)
        layout.addWidget(self.finish_email_button)
        self.recovery_result = self._note("")
        self.recovery_result.setTextFormat(Qt.TextFormat.PlainText)
        self.recovery_result.setVisible(False)
        layout.addWidget(self.recovery_result)
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
        self.email_login_compat = QCheckBox(
            self._t("기존 이메일 계정", "Legacy email account"), page
        )
        self.email_login_compat.toggled.connect(self._update_login_mode)
        layout.addWidget(self.email_login_compat)
        self.login_email = QLineEdit(page)
        self.login_email.setObjectName("loginEmail")
        self.login_password = QLineEdit(page)
        self.login_password.setObjectName("loginPassword")
        self.login_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.login_identity_label = QLabel(self._t("아이디", "Username"), page)
        form.addRow(self.login_identity_label, self.login_email)
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
        recover = QPushButton(self._t("복구 코드로 비밀번호 재설정", "Reset with recovery code"), page)
        recover.clicked.connect(self.show_recover)
        layout.addWidget(recover)
        return page

    def _build_recover_page(self):
        page, layout = self._detail_page(
            self._t("계정 복구", "Recover account")
        )
        layout.addWidget(self._note(self._t(
            "계정을 만들 때 표시된 복구 코드가 필요합니다.",
            "Use the recovery code shown when the account was created.",
        )))
        form = QFormLayout()
        self.recover_username = QLineEdit(page)
        self.recover_code = QLineEdit(page)
        self.recover_password = QLineEdit(page)
        self.recover_password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow(self._t("아이디", "Username"), self.recover_username)
        form.addRow(self._t("복구 코드", "Recovery code"), self.recover_code)
        form.addRow(self._t("새 비밀번호", "New password"), self.recover_password)
        layout.addLayout(form)
        self.recover_acknowledge = QCheckBox(
            self._t(
                "현재 익명 기록은 복구한 계정과 자동으로 합쳐지지 않음을 확인했습니다.",
                "I understand the current anonymous records will not be merged automatically.",
            ),
            page,
        )
        layout.addWidget(self.recover_acknowledge)
        self.recover_error = self._error_label()
        layout.addWidget(self.recover_error)
        layout.addStretch(1)
        row, self.recover_back, self.recover_submit = self._back_row(
            page, self._t("재설정", "Reset"), self.recover_account
        )
        self.recover_back.clicked.disconnect()
        self.recover_back.clicked.connect(self.show_login)
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
        self._refresh_record_status()
        if signature != self._controller_signature:
            self._refresh_room_section()
            self._refresh_account_page()
        self._controller_signature = signature

    def _current_controller_signature(self):
        online = self.controller.online
        group = online.get("group") or {}
        return (
            bool(self.controller._access_token()),
            group.get("id"),
            group.get("name"),
            group.get("invite_code"),
            group.get("owner_id"),
            group.get("time_zone"),
            group.get("day_start_hour"),
            online.get("display_name"),
            online.get("account_kind"),
            online.get("email"),
            online.get("username"),
            online.get("pending_email"),
            online.get("last_error"),
        )

    def _poll_controller(self):
        if not self._valid():
            self.refresh_timer.stop()
            return
        signature = self._current_controller_signature()
        self._refresh_record_status()
        if signature == self._controller_signature:
            return
        # Refresh only server-backed portions. Goal and language drafts remain intact.
        self.display_code_label.setText(self._display_code())
        self.account_code.setText(self._display_code())
        self._refresh_room_section()
        self._refresh_account_page()
        self._controller_signature = signature

    def _refresh_room_section(self):
        _clear_layout(self.room_layout)
        _clear_layout(self.members_list_layout)
        self.members_section.setVisible(False)
        self._members_loaded_context = None
        self.rotate_invite_button = None
        group = self.controller.online.get("group")
        if group:
            time_zone = str(group.get("time_zone") or DEFAULT_TIME_ZONE)
            day_label = f"{time_zone} · {DAY_START_HOUR:02d}:00"
            name = QLabel(
                str(group.get("name") or self._t("친구 그룹", "Study room")), self
            )
            name.setTextFormat(Qt.TextFormat.PlainText)
            font = name.font()
            font.setBold(True)
            name.setFont(font)
            name.setWordWrap(True)
            name.setToolTip(self._t(
                f"방 시간대 {time_zone} · 매일 {DAY_START_HOUR:02d}:00 기록 초기화",
                f"Room time zone {time_zone} · records reset daily at {DAY_START_HOUR:02d}:00",
            ))
            self.room_layout.addWidget(name)
            day = QLabel(day_label, self)
            day.setToolTip(name.toolTip())
            self.room_layout.addWidget(day)
            code_row = QHBoxLayout()
            code = str(group.get("invite_code") or "—")
            self.room_invite_code = QLabel(code, self)
            self.copy_invite_button = QPushButton(
                self._t("코드 복사", "Copy code"), self
            )
            self.copy_invite_button.setObjectName("copyInviteCode")
            self.copy_invite_message_button = QPushButton(
                self._t("초대 복사", "Copy invite"), self
            )
            self.copy_invite_message_button.setObjectName("copyInviteMessage")
            self.copy_invite_button.setEnabled(code != "—")
            self.copy_invite_message_button.setEnabled(code != "—")
            self.copy_invite_button.clicked.connect(self.copy_invite_code)
            self.copy_invite_message_button.clicked.connect(self.copy_invite_message)
            code_row.addWidget(QLabel(self._t("초대 코드", "Invite code"), self))
            code_row.addWidget(self.room_invite_code)
            code_row.addStretch(1)
            self.room_layout.addLayout(code_row)
            copy_row = QHBoxLayout()
            copy_row.addWidget(self.copy_invite_button)
            copy_row.addWidget(self.copy_invite_message_button)
            copy_row.addStretch(1)
            self.room_layout.addLayout(copy_row)
            user_id = (self.controller.online.get("auth") or {}).get("user_id")
            if user_id and group.get("owner_id") == user_id:
                owner_actions = QHBoxLayout()
                self.rotate_invite_button = QPushButton(
                    self._t("초대 코드 변경", "Change invite code"), self
                )
                self.rotate_invite_button.clicked.connect(self.rotate_invite_code)
                owner_actions.addWidget(self.rotate_invite_button)
                owner_actions.addStretch(1)
                self.room_layout.addLayout(owner_actions)
            leave_row = QHBoxLayout()
            leave_row.addStretch(1)
            leave = QPushButton(self._t("방 나가기", "Leave room"), self)
            leave.clicked.connect(self.show_leave)
            leave_row.addWidget(leave)
            self.room_layout.addLayout(leave_row)
            self.members_section.setVisible(True)
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
        username = self.controller.online.get("username")
        token = self.controller._access_token()
        if kind == "username" and username:
            self.account_summary.setText(
                self._t(f"통합 계정\n{username}", f"Synced account\n{username}")
            )
            self.link_email_button.setText(self._t("복구 코드 재발급", "Replace recovery code"))
            self.link_email_button.setVisible(bool(token))
            self.login_button.setText(self._t("다른 계정으로 전환", "Switch account"))
            self.login_button.setVisible(True)
        elif kind == "email" and email:
            self.account_summary.setText(
                self._t(f"연결된 이메일\n{email}", f"Linked email\n{email}")
            )
            self.link_email_button.setText(self._t("통합 계정 만들기", "Create synced account"))
            self.link_email_button.setVisible(bool(token))
            self.login_button.setText(self._t("다른 계정으로 전환", "Switch account"))
            self.login_button.setVisible(True)
        elif token:
            self.account_summary.setText(
                self._t(
                    "로그인 아이디 없음\n현재 기록은 이 PC에 보존됩니다.",
                    "No login ID\nYour current records are kept on this PC.",
                )
            )
            self.link_email_button.setVisible(True)
            self.link_email_button.setText(self._t("통합 계정 만들기", "Create synced account"))
            self.login_button.setVisible(True)
            self.login_button.setText(self._t("다른 PC의 계정으로 로그인", "Sign in on this PC"))
        else:
            self.account_summary.setText(
                self._t(
                    "계정 연결이 만료되었거나 아직 준비되지 않았습니다.",
                    "The account session has expired or is not ready yet.",
                )
            )
            self.link_email_button.setVisible(False)
            self.login_button.setVisible(True)
        expired_guest = bool(
            not token
            and self.controller.online.get("guest_id")
            and not username
            and not email
            and kind in (None, "guest")
        )
        self.restart_guest_button.setVisible(expired_guest)

    def restart_expired_guest(self):
        answer = QMessageBox.question(
            self,
            self._t("새 익명 계정", "New guest account"),
            self._t(
                "새 고유번호를 발급합니다. 이전 방은 복구되지 않지만 Anki 복습 기록과 목표는 그대로 남습니다.",
                "Create a new identity. The previous room cannot be restored, but Anki review history and goals are kept.",
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            restarted = self.controller.restart_expired_guest()
        except Exception as error:
            self._set_message(
                self.account_error,
                self._t(
                    f"새 계정을 시작하지 못했습니다.\n{error}",
                    f"Could not start a new account.\n{error}",
                ),
            )
            return
        if restarted:
            self.show_home()

    def show_home(self):
        self.refresh_from_controller()
        self._show_page(self.PAGE_HOME)
        self._home_tab_changed(self.home_tabs.currentIndex())
        self._update_save_enabled()

    def _show_page(self, index):
        self.pages.setCurrentIndex(index)
        QTimer.singleShot(0, self._scroll_to_top)

    def _scroll_to_top(self):
        if not self._valid() or self.pages.currentIndex() != self.PAGE_HOME:
            return
        current = self.home_tabs.currentWidget()
        if isinstance(current, QScrollArea):
            current.verticalScrollBar().setValue(0)

    def _home_tab_changed(self, index):
        settings_selected = index == self.TAB_SETTINGS
        self.home_buttons.setVisible(settings_selected)
        self.close_button.setVisible(not settings_selected)
        if index == self.TAB_ROOM and self.controller.online.get("group"):
            QTimer.singleShot(0, self._load_members_if_needed)

    def _load_members_if_needed(self):
        group = self.controller.online.get("group") or {}
        auth = self.controller.online.get("auth") or {}
        context = (
            str(group.get("id") or ""),
            str(auth.get("user_id") or ""),
            str(group.get("owner_id") or ""),
        )
        if not context[0] or not context[1] or context == self._members_loaded_context:
            return
        self.load_members()

    def show_record_status(self):
        self.show_home()
        self.home_tabs.setCurrentIndex(self.TAB_SETTINGS)
        self.record_status_toggle.setChecked(True)
        self.record_status_toggle.setFocus()

    def _toggle_record_status(self, expanded):
        self.record_status_toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self.record_status_details.setVisible(bool(expanded))

    @staticmethod
    def _status_time_text(value, time_zone):
        if value is None:
            return "—"
        zone = QTimeZone(str(time_zone or "UTC").encode("utf-8"))
        if not zone.isValid():
            zone = QTimeZone(b"UTC")
        moment = QDateTime.fromSecsSinceEpoch(int(float(value)), zone)
        current = QDateTime.currentDateTimeUtc().toTimeZone(zone)
        pattern = "HH:mm" if moment.date() == current.date() else "MM-dd HH:mm"
        return moment.toString(pattern)

    def _refresh_record_status(self):
        snapshot_method = getattr(self.controller, "record_status_snapshot", None)
        if not callable(snapshot_method):
            return
        snapshot = snapshot_method() or {}
        group = self.controller.online.get("group") or {}
        has_room = bool(group.get("id"))
        time_zone = str(
            group.get("time_zone")
            or _time_zone_text(QTimeZone.systemTimeZoneId())
            or DEFAULT_TIME_ZONE
        )
        tooltip = self._t(
            f"방 시간대: {time_zone}", f"Room time zone: {time_zone}"
        )
        for key, (title, value) in self.record_status_rows.items():
            remote = key in ("upload_at", "members_at")
            title.setVisible(has_room or not remote)
            value.setVisible(has_room or not remote)
            if remote and not has_room:
                continue
            stamp = snapshot.get(key)
            text = self._status_time_text(stamp, time_zone)
            if key == "upload_at" and int(snapshot.get("pending_count") or 0) > 0:
                text = self._t("대기", "Pending")
                success_text = self._status_time_text(stamp, time_zone)
                value.setToolTip(self._t(
                    f"마지막 공유 {success_text} · {tooltip}",
                    f"Last shared {success_text} · {tooltip}",
                ))
            else:
                value.setToolTip(tooltip)
            value.setText(text)
        error_names = {
            "local_save": self._t("기록 저장 실패", "Could not save records"),
            "local_read": self._t("기록 확인 실패", "Could not read records"),
            "upload": self._t("공유 지연", "Upload delayed"),
            "members": self._t("조회 지연", "Refresh delayed"),
        }
        errors = snapshot.get("errors") or {}
        visible_errors = [
            error_names[key] for key in ("local_save", "local_read", "upload", "members")
            if key in errors
        ]
        self.record_status_errors.setText("\n".join(visible_errors))
        self.record_status_errors.setToolTip(
            "\n".join(str(errors[key]) for key in errors if errors.get(key))
        )
        self.record_status_errors.setVisible(bool(visible_errors))
        self.record_status_zone.setText(
            self._t(f"기준 시간대 · {time_zone}", f"Time zone · {time_zone}")
            if has_room else self._t("이 PC 시간 기준", "This PC's local time")
        )
        self.record_status_mobile.setVisible(has_room)

    def show_create(self):
        self._set_message(self.create_error, "")
        self._show_page(self.PAGE_CREATE)
        self.group_name.setFocus()

    def show_join(self):
        self._set_message(self.join_error, "")
        self._show_page(self.PAGE_JOIN)
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
        self._show_page(self.PAGE_LEAVE)
        self.leave_submit.setFocus()

    def _owner_context(self):
        group = self.controller.online.get("group") or {}
        user_id = (self.controller.online.get("auth") or {}).get("user_id")
        if not group.get("id") or not user_id or group.get("owner_id") != user_id:
            return None
        return str(group["id"]), str(user_id)

    def show_members(self):
        self.show_home()
        self.home_tabs.setCurrentIndex(self.TAB_ROOM)
        self.load_members()

    def _same_owner_context(self, group_id, owner_id):
        return self._owner_context() == (group_id, owner_id)

    def load_members(self):
        group = self.controller.online.get("group") or {}
        current_user = (self.controller.online.get("auth") or {}).get("user_id")
        if not group.get("id") or not current_user:
            self.members_section.setVisible(False)
            return
        if not self._begin_remote(self.members_error):
            return
        _clear_layout(self.members_list_layout)
        self.members_list_layout.addWidget(
            self._note(self._t("멤버를 불러오는 중…", "Loading members…"))
        )
        group_id = str(group["id"])
        current_user = str(current_user)
        owner_id = str(group.get("owner_id") or "")
        expected_context = (group_id, current_user, owner_id)

        def operation(token):
            members = self.controller.client.list_group_members(token, group_id)
            bans = (
                self.controller.client.list_group_bans(token, group_id)
                if current_user == owner_id
                else []
            )
            return members, bans

        def success(value):
            self._finish_remote()
            current_group = self.controller.online.get("group") or {}
            current_auth = self.controller.online.get("auth") or {}
            current_context = (
                str(current_group.get("id") or ""),
                str(current_auth.get("user_id") or ""),
                str(current_group.get("owner_id") or ""),
            )
            if not self._valid():
                return
            if current_context != expected_context:
                _clear_layout(self.members_list_layout)
                return
            members, bans = value
            self._members_loaded_context = expected_context
            self._render_members(
                group_id, current_user, owner_id, members, bans
            )

        controls = self.members_section.findChildren(QPushButton)
        self.controller._run_authenticated_action(
            controls,
            operation,
            success,
            self._t("멤버를 불러오지 못했습니다.", "Could not load members."),
            on_error=self._remote_error(
                self.members_error,
                self._t("멤버를 불러오지 못했습니다.", "Could not load members."),
            ),
        )

    def _member_name(self, member):
        return str(
            member.get("display_name")
            or canonical_nickname(str(member.get("user_id") or ""))
        )

    def _render_members(
        self, group_id, current_user, owner_id, members, bans
    ):
        _clear_layout(self.members_list_layout)
        blocked_ids = {str(row.get("user_id")) for row in bans}
        known = {
            str(row.get("user_id")): self._member_name(row)
            for row in members
            if row.get("user_id")
        }
        if members:
            for member in members:
                target_user = str(member.get("user_id"))
                row = QHBoxLayout()
                name = self._member_name(member)
                suffix = ""
                if target_user == owner_id:
                    suffix = self._t(" · 방장", " · Owner")
                elif target_user == current_user:
                    suffix = self._t(" · 나", " · You")
                label = QLabel(name + suffix, self.members_section)
                label.setTextFormat(Qt.TextFormat.PlainText)
                row.addWidget(label)
                row.addStretch(1)
                if current_user == owner_id and target_user != owner_id:
                    remove = QPushButton(
                        self._t("내보내기", "Remove"), self.members_section
                    )
                    remove.clicked.connect(
                        lambda _checked=False, user=target_user, member_name=name:
                        self.block_member(group_id, owner_id, user, member_name)
                    )
                    row.addWidget(remove)
                self.members_list_layout.addLayout(row)
        else:
            self.members_list_layout.addWidget(
                self._note(self._t("멤버가 없습니다.", "No members."))
            )
        if current_user == owner_id and blocked_ids:
            self.members_list_layout.addWidget(
                self._heading(self._t("차단된 계정", "Blocked accounts"))
            )
            for target_user in sorted(blocked_ids):
                row = QHBoxLayout()
                label = QLabel(
                    known.get(target_user) or canonical_nickname(target_user),
                    self.members_section,
                )
                label.setTextFormat(Qt.TextFormat.PlainText)
                unblock = QPushButton(
                    self._t("차단 해제", "Unblock"), self.members_section
                )
                unblock.clicked.connect(
                    lambda _checked=False, user=target_user:
                    self.unblock_member(group_id, owner_id, user)
                )
                row.addWidget(label)
                row.addStretch(1)
                row.addWidget(unblock)
                self.members_list_layout.addLayout(row)

    def _confirm_member_removal(self, name):
        answer = QMessageBox.question(
            self,
            self._t("멤버 내보내기", "Remove member"),
            self._t(
                f"{name} 계정을 방에서 내보내고 다시 참여하지 못하게 차단합니다. "
                "이 방에 공유된 해당 계정의 기록은 삭제되지만, 그 사용자의 Anki 개인 기록은 그대로 남습니다.",
                f"Remove and block {name} from this room. Their records shared with this room "
                "will be removed, while their personal Anki records remain intact.",
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def block_member(self, group_id, owner_id, target_user, name):
        if not self._same_owner_context(group_id, owner_id):
            self.show_home()
            return
        if not self._confirm_member_removal(name):
            return
        self._moderate_member(group_id, owner_id, target_user, True)

    def unblock_member(self, group_id, owner_id, target_user):
        if not self._same_owner_context(group_id, owner_id):
            self.show_home()
            return
        self._moderate_member(group_id, owner_id, target_user, False)

    def _moderate_member(self, group_id, owner_id, target_user, blocked):
        if not self._begin_remote(self.members_error):
            return

        def operation(token):
            self.controller.client.moderate_group_member(
                token, group_id, target_user, blocked=blocked
            )
            return (
                self.controller.client.list_group_members(token, group_id),
                self.controller.client.list_group_bans(token, group_id),
            )

        def success(value):
            self._finish_remote()
            if not self._valid() or not self._same_owner_context(group_id, owner_id):
                return
            members, bans = value
            self.controller.online.pop("members", None)
            self.controller.sync_async(force=True)
            self._render_members(
                group_id, owner_id, owner_id, members, bans
            )

        controls = self.members_section.findChildren(QPushButton)
        self.controller._run_authenticated_action(
            controls,
            operation,
            success,
            self._t("멤버 설정을 바꾸지 못했습니다.", "Could not update the member."),
            on_error=self._remote_error(
                self.members_error,
                self._t("멤버 설정을 바꾸지 못했습니다.", "Could not update the member."),
            ),
        )

    def show_account(self):
        self.show_home()
        self.home_tabs.setCurrentIndex(self.TAB_ACCOUNT)

    def show_email(self):
        self._set_message(self.email_error, "")
        self.refresh_from_controller()
        replacing = self.controller.online.get("account_kind") == "username"
        self.recovery_result.setVisible(False)
        self.finish_email_button.setVisible(True)
        self.email_address.setText(
            str(self.controller.online.get("username") or "") if replacing else ""
        )
        self.email_address.setEnabled(not replacing)
        self.new_password.setEnabled(True)
        self.new_password.clear()
        self.finish_email_button.setText(
            self._t("복구 코드 재발급", "Replace recovery code")
            if replacing
            else self._t("계정 만들기", "Create account")
        )
        self._show_page(self.PAGE_EMAIL)
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
        self.email_login_compat.setChecked(False)
        self._update_login_enabled()
        self._show_page(self.PAGE_LOGIN)
        self.login_email.setFocus()

    def show_recover(self):
        self._set_message(self.recover_error, "")
        active_guest = (
            self.controller.online.get("account_kind") == "guest"
            and bool(self.controller._access_token())
        )
        self.recover_acknowledge.setVisible(active_guest)
        self.recover_acknowledge.setChecked(False)
        self._show_page(self.PAGE_RECOVER)
        self.recover_username.setFocus()

    def _update_login_mode(self, checked):
        self.login_identity_label.setText(
            self._t("이메일", "Email") if checked else self._t("아이디", "Username")
        )

    @staticmethod
    def _normalized_username(value):
        return str(value or "").strip().lower()

    def _username_error(self, username):
        if not re.fullmatch(r"[a-z0-9_]{4,24}", username):
            return self._t(
                "아이디는 영문 소문자·숫자·밑줄 4~24자로 입력해 주세요.",
                "Use 4–24 lowercase letters, digits, or underscores.",
            )
        return ""

    def _password_error(self, password):
        if len(password) < 10:
            return self._t(
                "비밀번호는 10자 이상으로 입력해 주세요.",
                "Use at least 10 characters for the password.",
            )
        if len(password.encode("utf-8")) > 72:
            return self._t(
                "비밀번호는 UTF-8 기준 72바이트 이하여야 합니다.",
                "The password must be at most 72 UTF-8 bytes.",
            )
        return ""

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
            or self.share_deck_name.isChecked() != self._share_deck_at_open
        )

    def _update_save_enabled(self, *_args):
        self.save_button.setEnabled(self._has_changes())

    def _reset_draft(self):
        self._time_at_open = self.controller.tracker.time_goal_minutes
        self._answers_at_open = self.controller.tracker.card_goal
        self._locale_at_open = self.controller.locale
        self.time_goal.setValue(self._time_at_open)
        self.answer_goal.setValue(self._answers_at_open)
        self._share_deck_at_open = bool(
            self.controller.online.get("share_deck_name", True)
        )
        self.share_deck_name.setChecked(self._share_deck_at_open)
        index = self.language.findData(self._locale_at_open)
        self.language.setCurrentIndex(max(index, 0))
        self._update_save_enabled()

    def save_settings(self):
        changes = {}
        # Only submit fields edited in this dialog. Values changed from the panel
        # while this window was open must not be replaced by this stale draft.
        if self.time_goal.value() != self._time_at_open:
            changes["time_goal_minutes"] = validate_goal(
                self.time_goal.value(), maximum=TIME_GOAL_MAX_MINUTES
            )
        if self.answer_goal.value() != self._answers_at_open:
            changes["card_goal"] = validate_goal(
                self.answer_goal.value(), maximum=ANSWER_GOAL_MAX
            )
        if self.share_deck_name.isChecked() != self._share_deck_at_open:
            changes["share_deck_name"] = self.share_deck_name.isChecked()
        if self.language.currentData() != self._locale_at_open:
            locale = self.language.currentData()
            changes["locale"] = locale if locale in ("ko", "en") else "ko"
        try:
            self.controller.update_local_settings(**changes)
        except Exception as error:
            self._set_message(
                self.home_error,
                self._t(
                    f"설정을 저장하지 못했습니다.\n{error}",
                    f"Could not save settings.\n{error}",
                ),
            )
            return
        self.accept()

    def _confirm_invite_rotation(self):
        answer = QMessageBox.question(
            self,
            self._t("초대 코드 변경", "Replace invite code"),
            self._t(
                "새 코드를 만들면 기존 초대 코드는 즉시 사용할 수 없습니다. "
                "현재 멤버와 기록은 그대로 유지됩니다.",
                "Generating a new code immediately invalidates the old invite code. "
                "Current members and records are kept.",
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def rotate_invite_code(self):
        group = dict(self.controller.online.get("group") or {})
        group_id = group.get("id")
        user_id = (self.controller.online.get("auth") or {}).get("user_id")
        if (
            not group_id
            or not user_id
            or group.get("owner_id") != user_id
            or self.rotate_invite_button is None
        ):
            return
        if not self._confirm_invite_rotation():
            return
        if not self._begin_remote(self.home_error):
            return

        def success(code):
            self._finish_remote()
            code = str(code or "").strip().upper()
            allowed = set("23456789ABCDEFGHJKLMNPQRSTUVWXYZ")
            if len(code) != 4 or any(character not in allowed for character in code):
                if self._valid():
                    self._set_message(
                        self.home_error,
                        self._t(
                            "새 초대 코드를 받지 못했습니다.",
                            "The new invite code was not returned.",
                        ),
                    )
                return
            current = self.controller.online.get("group") or {}
            if current.get("id") == group_id:
                current["invite_code"] = code
                self.controller.save()
                if self._valid():
                    self.refresh_from_controller()

        self.controller._run_authenticated_action(
            [self.rotate_invite_button],
            lambda token: self.controller.client.rotate_invite(token, group_id),
            success,
            self._t(
                "초대 코드를 바꾸지 못했습니다.",
                "Could not replace the invite code.",
            ),
            on_error=self._remote_error(
                self.home_error,
                self._t(
                    "초대 코드를 바꾸지 못했습니다.",
                    "Could not replace the invite code.",
                ),
            ),
        )

    def copy_invite_code(self):
        code = self.room_invite_code.text()
        if not code or code == "—":
            return
        QApplication.clipboard().setText(code)
        self._show_copy_feedback(
            self.copy_invite_button, self._t("코드 복사", "Copy code")
        )

    def copy_invite_message(self):
        try:
            message = self.controller.invite_message()
        except (TypeError, ValueError) as error:
            self._set_message(self.home_error, str(error))
            return
        QApplication.clipboard().setText(message)
        self._show_copy_feedback(
            self.copy_invite_message_button,
            self._t("초대 복사", "Copy invite"),
        )

    def _show_copy_feedback(self, button, normal_text):
        generation = self._copy_feedback_generation.get(button, 0) + 1
        self._copy_feedback_generation[button] = generation
        button.setMinimumWidth(max(button.minimumWidth(), button.sizeHint().width()))
        button.setText(self._t("복사됨", "Copied"))
        QTimer.singleShot(
            2000,
            lambda: self._clear_copy_feedback(button, normal_text, generation),
        )

    def _clear_copy_feedback(self, button, normal_text, generation):
        if not self._valid() or self._copy_feedback_generation.get(button) != generation:
            return
        try:
            button.setText(normal_text)
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
        try:
            name = normalize_room_name(self.group_name.text())
        except ValueError:
            self._set_message(
                self.create_error,
                self._t("방 이름을 입력해 주세요.", "Enter a room name."),
            )
            return
        time_zone = self.room_time_zone.currentText().strip()
        if time_zone not in _available_time_zones():
            self._set_message(
                self.create_error,
                self._t("목록에서 시간대를 선택해 주세요.", "Choose a time zone from the list."),
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
            group = dict(group)
            group.setdefault(
                "owner_id",
                (self.controller.online.get("auth") or {}).get("user_id"),
            )
            self.controller.online["group"] = group
            _apply_group_time_zone(self.controller, group)
            self.controller.save()
            self.controller.sync_async(force=True)
            if self._valid():
                self.show_home()

        self.controller._run_authenticated_action(
            [self.create_submit],
            lambda token: self.controller.client.create_group(token, name, time_zone),
            success,
            self._t("방을 만들지 못했습니다.", "Could not create the room."),
            on_error=self._remote_error(
                self.create_error,
                self._t("방을 만들지 못했습니다.", "Could not create the room."),
            ),
        )

    def join_group(self):
        try:
            code = validate_invite_code(self.invite_code.text())
        except ValueError:
            code = "".join(self.invite_code.text().split()).upper()
            self.invite_code.setText(code)
            self._set_message(
                self.join_error,
                self._t(
                    "초대 코드는 영문 대문자와 숫자 4자리입니다.",
                    "The invite code is 4 uppercase letters or digits.",
                ),
            )
            return
        self.invite_code.setText(code)
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
            group = next(
                (item for item in groups if item.get("id") == group_id),
                {
                    "id": group_id,
                    "name": self._t("친구 그룹", "Study room"),
                    "invite_code": code,
                },
            )
            self.controller.online["group"] = group
            _apply_group_time_zone(self.controller, group)
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

        user_id = (self.controller.online.get("auth") or {}).get("user_id")

        def success(_result):
            self._finish_remote()
            current = self.controller.online.get("group") or {}
            if current.get("id") == group_id:
                if user_id and hasattr(self.controller, "leave_current_room_locally"):
                    self.controller.leave_current_room_locally(user_id, group_id)
                else:
                    if user_id and hasattr(self.controller, "discard_room_outbox"):
                        self.controller.discard_room_outbox(user_id, group_id)
                    self.controller.online.pop("group", None)
                    self.controller.online.pop("members", None)
                    _apply_group_time_zone(self.controller, None)
                    self.controller.save()
                    self.controller.refresh()
            if self._valid():
                self.show_home()

        def operation(token):
            try:
                return self.controller.client.leave_group(token, str(group_id))
            except SupabaseError as error:
                if _already_left_error(error):
                    return {"already_left": True}
                raise

        self.controller._run_authenticated_action(
            [self.leave_back, self.leave_submit],
            operation,
            success,
            self._t("방에서 나가지 못했습니다.", "Could not leave the room."),
            on_error=self._remote_error(
                self.leave_error,
                self._t("방에서 나가지 못했습니다.", "Could not leave the room."),
            ),
        )

    def send_verification_email(self):
        self.finish_email_link()

    def finish_email_link(self):
        username = self._normalized_username(self.email_address.text())
        self.email_address.setText(username)
        password = self.new_password.text()
        validation_error = self._username_error(username) or self._password_error(password)
        if validation_error:
            self._set_message(self.email_error, validation_error)
            return
        if not self._begin_remote(self.email_error):
            return

        def operation(token):
            current_user_id = (self.controller.online.get("auth") or {}).get(
                "user_id"
            )
            if not current_user_id:
                raise SupabaseError(
                    self._t(
                        "현재 계정을 확인할 수 없습니다. Anki를 다시 시작한 뒤 시도해 주세요.",
                        "The current account could not be verified. Restart Anki and try again.",
                    )
                )
            session = self.controller.client.bind_username(token, username, password)
            user = session.get("user") or {}
            verified_user_id = user.get("id")
            if verified_user_id != current_user_id:
                raise SupabaseError(
                    self._t(
                        "인증된 계정이 현재 계정과 다릅니다. 연결을 중단했습니다.",
                        "The verified account is different from the current account. Linking was stopped.",
                    )
                )
            if session.get("username") != username:
                raise SupabaseError(
                    self._t(
                        "서버에서 다른 아이디를 반환해 연결을 중단했습니다.",
                        "The server returned a different username. Linking was stopped.",
                    )
                )
            if not session.get("access_token") or not session.get("recovery_code"):
                raise SupabaseError(
                    self._t(
                        "계정 또는 복구 코드 정보를 받지 못했습니다.",
                        "The account or recovery code was not returned.",
                    )
                )
            return session

        def success(session):
            self._finish_remote()
            user = session.get("user") or {}
            display_name = self.controller.online.get("display_name") or canonical_nickname(
                user["id"]
            )
            self.controller._store_session(session, "", display_name)
            self.controller.online["account_kind"] = "username"
            self.controller.online["username"] = username
            self.controller.online.pop("email", None)
            self.controller.online.pop("pending_email", None)
            self.controller.save()
            self.controller.sync_async(force=True)
            if self._valid():
                self.new_password.clear()
                self.email_address.setEnabled(False)
                self.new_password.setEnabled(False)
                self.finish_email_button.setVisible(False)
                self.recovery_result.setText(self._t(
                    f"복구 코드\n{session['recovery_code']}\n\n이 코드는 다시 표시되지 않습니다. 지금 안전한 곳에 보관하세요.",
                    f"Recovery code\n{session['recovery_code']}\n\nThis code will not be shown again. Store it safely now.",
                ))
                self.recovery_result.setVisible(True)

        self.controller._run_authenticated_action(
            [self.finish_email_button],
            operation,
            success,
            self._t(
                "통합 계정을 만들지 못했습니다.",
                "Could not create the synced account.",
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
        legacy_email = self.email_login_compat.isChecked()
        address = self.login_email.text().strip()
        if not legacy_email:
            address = self._normalized_username(address)
            self.login_email.setText(address)
        password = self.login_password.text()
        if not address or not password:
            self._set_message(
                self.login_error,
                self._t("이메일과 비밀번호를 입력해 주세요.", "Enter your email and password.")
                if legacy_email
                else self._t("아이디와 비밀번호를 입력해 주세요.", "Enter your username and password."),
            )
            return
        validation_error = "" if legacy_email else (
            self._username_error(address) or self._password_error(password)
        )
        if validation_error:
            self._set_message(self.login_error, validation_error)
            return
        if not self._begin_remote(self.login_error):
            return
        self.controller._cancel_identity_bootstrap()

        def task():
            result = (
                self.controller.client.sign_in(address, password)
                if legacy_email
                else self.controller.client.sign_in_username(address, password)
            )
            user = result.get("user") or {}
            user_id = user.get("id") or result.get("user_id")
            if not user_id:
                raise SupabaseError(
                    self._t(
                        "로그인 응답에서 사용자 정보를 찾지 못했습니다.",
                        "The sign-in response did not include a user.",
                    )
                )
            if not legacy_email and result.get("username") != address:
                raise SupabaseError(self._t(
                    "로그인 응답의 아이디가 일치하지 않습니다.",
                    "The username in the sign-in response did not match.",
                ))
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
                        "username",
                        "email",
                        "pending_email",
                        "group",
                        "members",
                    )
                    if key in previous
                }
            self.controller.online.pop("group", None)
            self.controller.online.pop("members", None)
            self.controller._store_session(result, address if legacy_email else "", name)
            self.controller.online["account_kind"] = "email" if legacy_email else "username"
            if legacy_email:
                self.controller.online.pop("username", None)
            else:
                self.controller.online["username"] = address
                self.controller.online.pop("email", None)
            self.controller.online.pop("pending_email", None)
            if groups:
                self.controller.online["group"] = groups[0]
            _apply_group_time_zone(
                self.controller, self.controller.online.get("group")
            )
            self.controller.save()
            self.controller.sync_async(force=True)
            if self._valid():
                self.login_password.clear()
                self._reset_draft()
                recovery_code = result.get("recovery_code")
                if recovery_code:
                    self.show_account()
                    self._set_message(self.account_error, self._t(
                        f"계정 연결 완료\n복구 코드: {recovery_code}\n이 코드는 다시 표시되지 않습니다. 지금 안전한 곳에 보관하세요.",
                        f"Account connected\nRecovery code: {recovery_code}\nThis code will not be shown again. Store it safely now.",
                    ))
                else:
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

    def recover_account(self):
        active_guest = (
            self.controller.online.get("account_kind") == "guest"
            and bool(self.controller._access_token())
        )
        if active_guest and not self.recover_acknowledge.isChecked():
            self._set_message(
                self.recover_error,
                self._t(
                    "계정 전환 안내를 확인해 주세요.",
                    "Confirm the account-switching notice first.",
                ),
            )
            return
        username = self._normalized_username(self.recover_username.text())
        self.recover_username.setText(username)
        recovery_code = self.recover_code.text().strip()
        password = self.recover_password.text()
        validation_error = self._username_error(username) or self._password_error(password)
        if validation_error:
            self._set_message(self.recover_error, validation_error)
            return
        if not recovery_code:
            self._set_message(
                self.recover_error,
                self._t("복구 코드를 입력해 주세요.", "Enter the recovery code."),
            )
            return
        if not self._begin_remote(self.recover_error):
            return
        self.controller._cancel_identity_bootstrap()

        def task():
            session = self.controller.client.recover_username(
                username, recovery_code, password
            )
            user = session.get("user") or {}
            user_id = user.get("id") or session.get("user_id")
            new_code = session.get("recovery_code")
            if not user_id or not session.get("access_token") or not new_code:
                raise SupabaseError(self._t(
                    "복구 응답이 완전하지 않습니다.",
                    "The recovery response is incomplete.",
                ))
            if session.get("username") != username:
                raise SupabaseError(self._t(
                    "복구 응답의 아이디가 일치하지 않습니다.",
                    "The username in the recovery response did not match.",
                ))
            name = canonical_nickname(user_id)
            self.controller.client.upsert_profile(
                session["access_token"], user_id, name
            )
            groups = self.controller.client.list_groups(session["access_token"], user_id)
            return session, groups, name

        def success(value):
            self._finish_remote()
            session, groups, name = value
            if (
                self.controller.online.get("account_kind") == "guest"
                and self.controller._access_token()
            ):
                previous = self.controller.online
                self.controller.online["guest_session_backup"] = {
                    key: deepcopy(previous[key])
                    for key in (
                        "auth",
                        "guest_id",
                        "display_name",
                        "account_kind",
                        "username",
                        "email",
                        "pending_email",
                        "group",
                        "members",
                    )
                    if key in previous
                }
            self.controller.online.pop("group", None)
            self.controller.online.pop("members", None)
            self.controller._store_session(session, "", name)
            self.controller.online["account_kind"] = "username"
            self.controller.online["username"] = username
            self.controller.online.pop("email", None)
            self.controller.online.pop("pending_email", None)
            if groups:
                self.controller.online["group"] = groups[0]
            _apply_group_time_zone(
                self.controller, self.controller.online.get("group")
            )
            self.controller.save()
            self.controller.sync_async(force=True)
            if self._valid():
                self.recover_code.clear()
                self.recover_password.clear()
                self._set_message(self.recover_error, self._t(
                    f"복구 완료\n새 복구 코드: {session['recovery_code']}\n이 코드를 지금 안전한 곳에 보관하세요.",
                    f"Recovered\nNew recovery code: {session['recovery_code']}\nStore this code safely now.",
                ))

        self.controller._run_online_action(
            [self.recover_back, self.recover_submit],
            task,
            success,
            self._t("계정을 복구하지 못했습니다.", "Could not recover the account."),
            on_error=self._remote_error(
                self.recover_error,
                self._t("계정을 복구하지 못했습니다.", "Could not recover the account."),
            ),
        )
