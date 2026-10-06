"""Internationalization (i18n) support for Study Companion.

Supports Korean (ko), English (en), Japanese (ja), and Simplified Chinese (zh_CN).
"""

from __future__ import annotations

from typing import Any

DEFAULT_LOCALE = "ko"
SUPPORTED_LOCALES = ("ko", "en", "ja", "zh_CN")

LOCALE_NAMES = {
    "ko": "한국어",
    "en": "English",
    "ja": "日本語",
    "zh_CN": "简体中文",
}

TRANSLATIONS: dict[str, dict[str, str]] = {
    "ko": {
        "change_timezone": "방 시간대 변경",
        "transfer_owner": "방장 위임",
        "kick_member": "내보내기",
        "cleanup_inactive": "비활성 멤버 정리 (14일 이상)",
        "member_management": "방장 전용 멤버 관리",
        "room_timezone": "방 시간대",
        "kicked_notification": "방에서 내보내졌습니다. 방 연결이 초기화되었습니다.",
        "transfer_owner_confirm": "'{name}' 님에게 방장 권한을 위임하시겠습니까?",
        "kick_member_confirm": "'{name}' 님을 방에서 내보내시겠습니까?",
        "cleanup_inactive_confirm": "14일 이상 미동기화된 멤버를 정리하시겠습니까?",
        "cleanup_inactive_result": "{count}명의 비활성 멤버를 정리했습니다.",
        "timezone_updated": "시간대가 변경되었습니다.",
        "ownership_transferred": "방장 권한이 위임되었습니다.",
        "member_kicked": "멤버를 내보냈습니다.",
        "owner_only_feature": "방장 전용 기능입니다.",
        "language": "언어",
        "save": "저장",
        "cancel": "취소",
        "confirm": "확인",
        "study_room": "스터디 룸",
        "owner": "방장",
        "you": "나",
        "no_members": "멤버가 없습니다.",
        "loading_members": "멤버를 불러오는 중…",
        "failed_load_members": "멤버를 불러오지 못했습니다.",
        "blocked_accounts": "차단된 계정",
        "unblock": "차단 해제",
        "public_room": "공개 방",
        "is_public_room": "공개 방으로 설정",
        "public_rooms_title": "공개 스터디방",
        "quick_join": "빠른 참여",
        "no_public_rooms": "참여 가능한 공개 방이 없습니다.",
        "refresh": "새로고침",
        "members_count": "{count}/{max}명",
        "studying_now": "{count}명 공부 중",
        "quick_join_failed": "참여 가능한 공개 방이 없습니다.",
        "one_room_limit_note": "* 1인당 1개 방만 참여할 수 있습니다.",
        "browse_other_rooms": "다른 공개 방 둘러보기",
        "switch_room_confirm": "새로운 방에 참여하면 현재 방({name})에서 나가게 됩니다.\n계속하시겠습니까?",
    },
    "en": {
        "change_timezone": "Change room time zone",
        "transfer_owner": "Transfer ownership",
        "kick_member": "Kick member",
        "cleanup_inactive": "Clean up inactive members (14+ days)",
        "member_management": "Owner member management",
        "room_timezone": "Room time zone",
        "kicked_notification": "You were removed from the room. Room connection has been reset.",
        "transfer_owner_confirm": "Transfer room ownership to '{name}'?",
        "kick_member_confirm": "Remove '{name}' from the room?",
        "cleanup_inactive_confirm": "Clean up members inactive for 14 or more days?",
        "cleanup_inactive_result": "Cleaned up {count} inactive member(s).",
        "timezone_updated": "Time zone updated.",
        "ownership_transferred": "Ownership transferred.",
        "member_kicked": "Member removed.",
        "owner_only_feature": "Owner-only feature.",
        "language": "Language",
        "save": "Save",
        "cancel": "Cancel",
        "confirm": "Confirm",
        "study_room": "Study room",
        "owner": "Owner",
        "you": "You",
        "no_members": "No members.",
        "loading_members": "Loading members…",
        "failed_load_members": "Could not load members.",
        "blocked_accounts": "Blocked accounts",
        "unblock": "Unblock",
        "public_room": "Public room",
        "is_public_room": "Make room public",
        "public_rooms_title": "Public Study Rooms",
        "quick_join": "Quick Join",
        "no_public_rooms": "No public rooms available.",
        "refresh": "Refresh",
        "members_count": "{count}/{max}",
        "studying_now": "{count} studying",
        "quick_join_failed": "No public rooms available to join.",
        "one_room_limit_note": "* Each user may join only 1 study room.",
        "browse_other_rooms": "Browse other rooms",
        "switch_room_confirm": "Joining a new room will leave your current room ({name}).\nDo you want to continue?",
    },
    "ja": {
        "change_timezone": "ルームのタイムゾーン変更",
        "transfer_owner": "オーナー権限を譲渡",
        "kick_member": "追放",
        "cleanup_inactive": "非アクティブメンバーを整理（14日以上）",
        "member_management": "オーナー専用メンバー管理",
        "room_timezone": "ルームのタイムゾーン",
        "kicked_notification": "ルームから退出させられました。ルーム接続がリセットされました。",
        "transfer_owner_confirm": "「{name}」さんにオーナー権限を譲渡しますか？",
        "kick_member_confirm": "「{name}」さんをルームから追放しますか？",
        "cleanup_inactive_confirm": "14日以上同期していないメンバーを整理しますか？",
        "cleanup_inactive_result": "{count}人の非アクティブメンバーを整理しました。",
        "timezone_updated": "タイムゾーンが変更されました。",
        "ownership_transferred": "オーナー権限を譲渡しました。",
        "member_kicked": "メンバーを追放しました。",
        "owner_only_feature": "オーナー専用機能です。",
        "language": "言語",
        "save": "保存",
        "cancel": "キャンセル",
        "confirm": "確認",
        "study_room": "スタディルーム",
        "owner": "オーナー",
        "you": "自分",
        "no_members": "メンバーがいません。",
        "loading_members": "メンバーを読み込み中…",
        "failed_load_members": "メンバーを読み込めませんでした。",
        "blocked_accounts": "ブロックされたアカウント",
        "unblock": "ブロック解除",
        "public_room": "公開ルーム",
        "is_public_room": "公開ルームに設定",
        "public_rooms_title": "公開スタディルーム",
        "quick_join": "クイック参加",
        "no_public_rooms": "参加可能な公開ルームがありません。",
        "refresh": "更新",
        "members_count": "{count}/{max}人",
        "studying_now": "{count}人が学習中",
        "quick_join_failed": "参加可能な公開ルームがありません。",
        "one_room_limit_note": "* 参加できるスタディルームは1人1部屋のみです。",
        "browse_other_rooms": "他の公開ルームを見る",
        "switch_room_confirm": "新しいルームに参加すると現在のルーム（{name}）から退出します。\nよろしいですか？",
    },
    "zh_CN": {
        "change_timezone": "更改房间时区",
        "transfer_owner": "移交房主权限",
        "kick_member": "移出成员",
        "cleanup_inactive": "清理不活跃成员（14天以上）",
        "member_management": "房主专用成员管理",
        "room_timezone": "房间时区",
        "kicked_notification": "您已被移出房间。房间连接已重置。",
        "transfer_owner_confirm": "确定将房主权限移交给“{name}”吗？",
        "kick_member_confirm": "确定将“{name}”移出房间吗？",
        "cleanup_inactive_confirm": "确定清理14天以上未同步的成员吗？",
        "cleanup_inactive_result": "已清理 {count} 名不活跃成员。",
        "timezone_updated": "时区已更新。",
        "ownership_transferred": "房主权限已移交。",
        "member_kicked": "已移出成员。",
        "owner_only_feature": "房主专用功能。",
        "language": "语言",
        "save": "保存",
        "cancel": "取消",
        "confirm": "确定",
        "study_room": "学习房间",
        "owner": "房主",
        "you": "我",
        "no_members": "暂无成员。",
        "loading_members": "正在加载成员…",
        "failed_load_members": "加载成员失败。",
        "blocked_accounts": "已屏蔽账户",
        "unblock": "解除屏蔽",
        "public_room": "公开房间",
        "is_public_room": "设为公开房间",
        "public_rooms_title": "公开学习房间",
        "quick_join": "快速加入",
        "no_public_rooms": "暂无可加入的公开房间。",
        "refresh": "刷新",
        "members_count": "{count}/{max}人",
        "studying_now": "{count}人正在学习",
        "quick_join_failed": "暂无可加入的公开房间。",
        "one_room_limit_note": "* 每人最多只能加入1个自习室。",
        "browse_other_rooms": "查看其他公开房间",
        "switch_room_confirm": "加入新房间将会退出当前房间（{name}）。\n是否继续？",
    },
}


def normalize_locale(locale: str | None) -> str:
    """Normalize locale string to one of the supported locales."""
    if not locale:
        return DEFAULT_LOCALE
    clean = str(locale).strip().replace("-", "_")
    if clean in SUPPORTED_LOCALES:
        return clean
    lower = clean.casefold()
    if lower.startswith("ko"):
        return "ko"
    if lower.startswith("ja"):
        return "ja"
    if lower.startswith("zh"):
        return "zh_CN"
    if lower.startswith("en"):
        return "en"
    return DEFAULT_LOCALE


def t(key: str, locale: str = DEFAULT_LOCALE, **kwargs: Any) -> str:
    """Look up a translated string by key and format with kwargs."""
    norm_locale = normalize_locale(locale)
    table = TRANSLATIONS.get(norm_locale, TRANSLATIONS[DEFAULT_LOCALE])
    template = table.get(key)
    if template is None:
        template = TRANSLATIONS[DEFAULT_LOCALE].get(key, key)
    if kwargs:
        try:
            return template.format(**kwargs)
        except Exception:
            return template
    return template


def tr(
    korean: str,
    english: str,
    japanese: str = "",
    chinese: str = "",
    locale: str = DEFAULT_LOCALE,
) -> str:
    """Helper to return string for the current locale among explicit alternatives."""
    norm = normalize_locale(locale)
    if norm == "en":
        return english or korean
    if norm == "ja":
        return japanese or english or korean
    if norm == "zh_CN":
        return chinese or english or korean
    return korean
