"""Small stdlib-only client for the add-on's Supabase backend."""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_URL = "https://uvrnsdknkivtclzlfjxx.supabase.co"
DEFAULT_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InV2cm5zZGtua2l2dGNsemxmanh4Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTQ0OTA2NzYsImV4cCI6MjA3MDA2NjY3Nn0."
    "k_jx9ysXfPIsU9ls3NHSgE4uqEkOrdzPftVVcYvck2s"
)


class SupabaseError(RuntimeError):
    """A request rejected by Supabase or unable to reach it."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class SupabaseClient:
    def __init__(
        self,
        url: str = DEFAULT_URL,
        key: str = DEFAULT_KEY,
        *,
        opener: Callable[..., Any] = urlopen,
        timeout: float = 15,
    ):
        self.url = url.rstrip("/")
        self.key = key
        self.opener = opener
        self.timeout = timeout

    def sign_up(self, email: str, password: str) -> dict[str, Any]:
        return self._request(
            "POST", "/auth/v1/signup", body={"email": email, "password": password}
        )

    def sign_in_anonymously(self) -> dict[str, Any]:
        return self._request("POST", "/auth/v1/signup", body={"data": {}})

    def sign_in(self, email: str, password: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/auth/v1/token",
            query={"grant_type": "password"},
            body={"email": email, "password": password},
        )

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/auth/v1/token",
            query={"grant_type": "refresh_token"},
            body={"refresh_token": refresh_token},
        )

    def get_user(self, token: str) -> dict[str, Any]:
        return self._request("GET", "/auth/v1/user", token=token)

    def update_user(self, token: str, **attributes: Any) -> dict[str, Any]:
        return self._request("PUT", "/auth/v1/user", token=token, body=attributes)

    def upsert_profile(
        self, token: str, user_id: str, display_name: str
    ) -> dict[str, Any] | None:
        result = self._request(
            "POST",
            "/rest/v1/profiles",
            token=token,
            query={"on_conflict": "id"},
            body={"id": user_id, "display_name": display_name},
            prefer="resolution=merge-duplicates,return=representation",
        )
        return self._first(result)

    def create_group(self, token: str, name: str) -> dict[str, Any] | None:
        result = self._request(
            "POST",
            "/rest/v1/rpc/create_study_group",
            token=token,
            body={"group_name": name},
        )
        created = self._first(result)
        if not created:
            return None
        return {
            "id": created.get("id") or created.get("group_id"),
            "name": name,
            "invite_code": created.get("invite_code"),
        }

    def join_group(self, token: str, code: str) -> Any:
        return self._request(
            "POST",
            "/rest/v1/rpc/join_study_group",
            token=token,
            body={"code": code},
        )

    def leave_group(self, token: str, group_id: str) -> Any:
        return self._request(
            "POST",
            "/rest/v1/rpc/leave_study_group",
            token=token,
            body={"target_group": group_id},
        )

    def list_groups(self, token: str, user_id: str) -> list[dict[str, Any]]:
        rows = self._request(
            "GET",
            "/rest/v1/group_members",
            token=token,
            query={
                "select": (
                    "group_id,joined_at,"
                    "study_groups(id,name,invite_code,owner_id,created_at)"
                ),
                "user_id": f"eq.{user_id}",
                "order": "joined_at.asc",
            },
        )
        groups = []
        for row in rows or []:
            group = dict(row.get("study_groups") or {})
            group.setdefault("id", row.get("group_id"))
            group["joined_at"] = row.get("joined_at")
            groups.append(group)
        return groups

    def upsert_daily_stats(
        self, token: str, rows: Iterable[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        result = self._request(
            "POST",
            "/rest/v1/daily_stats",
            token=token,
            query={"on_conflict": "group_id,user_id,study_day"},
            body=list(rows),
            prefer="resolution=merge-duplicates,return=representation",
        )
        return result or []

    def fetch_group_today(
        self, token: str, group_id: str, day: str
    ) -> list[dict[str, Any]]:
        memberships = self._request(
            "GET",
            "/rest/v1/group_members",
            token=token,
            query={
                "select": "user_id,joined_at",
                "group_id": f"eq.{group_id}",
                "order": "joined_at.asc",
            },
        ) or []
        stats = self._request(
            "GET",
            "/rest/v1/daily_stats",
            token=token,
            query={
                "select": (
                    "group_id,user_id,study_day,active_seconds,answer_count,"
                    "time_goal_minutes,card_goal,status,updated_at"
                ),
                "group_id": f"eq.{group_id}",
                "study_day": f"eq.{day}",
                "order": "updated_at.desc",
            },
        ) or []
        user_ids = list(dict.fromkeys(row["user_id"] for row in memberships))
        names: dict[str, str] = {}
        if user_ids:
            profiles = self._request(
                "GET",
                "/rest/v1/profiles",
                token=token,
                query={
                    "select": "id,display_name",
                    "id": f"in.({','.join(user_ids)})",
                },
            ) or []
            names = {row["id"]: row["display_name"] for row in profiles}

        stats_by_user = {row["user_id"]: row for row in stats}
        merged = []
        for membership in memberships:
            user_id = membership["user_id"]
            item = {
                "group_id": group_id,
                "user_id": user_id,
                "study_day": day,
                "active_seconds": 0,
                "answer_count": 0,
                "time_goal_minutes": 0,
                "card_goal": 0,
                "status": "stopped",
            }
            item.update(stats_by_user.get(user_id, {}))
            item["display_name"] = names.get(user_id)
            merged.append(item)
        return merged

    @staticmethod
    def _first(result: Any) -> Any:
        if isinstance(result, list):
            return result[0] if result else None
        return result

    def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        query: dict[str, str] | None = None,
        body: Any = None,
        prefer: str | None = None,
    ) -> Any:
        url = f"{self.url}{path}"
        if query:
            url = f"{url}?{urlencode(query, safe='(),.*')}"
        headers = {"apikey": self.key, "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if prefer:
            headers["Prefer"] = prefer
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url, data=data, headers=headers, method=method)
        try:
            response = self.opener(request, timeout=self.timeout)
            raw = response.read()
            close = getattr(response, "close", None)
            if close:
                close()
        except HTTPError as error:
            raw = error.read()
            detail = self._error_detail(raw) or error.reason
            raise SupabaseError(
                f"Supabase 요청 실패 ({error.code}): {detail}", status=error.code
            ) from error
        except (URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise SupabaseError(f"Supabase에 연결할 수 없습니다: {reason}") from error

        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SupabaseError("Supabase가 올바르지 않은 응답을 반환했습니다") from error

    @staticmethod
    def _error_detail(raw: bytes) -> str | None:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return raw.decode("utf-8", errors="replace").strip() or None
        if isinstance(payload, dict):
            for key in ("msg", "message", "error_description", "error", "details"):
                if payload.get(key):
                    return str(payload[key])
        return str(payload) if payload else None
