"""Small stdlib-only client for the add-on's Supabase backend."""

from __future__ import annotations

import json
import hashlib
import platform
import uuid
from pathlib import Path
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


class DeviceSnapshotConflict(SupabaseError):
    """A server snapshot proves that local sync state was restored or reset."""

    def __init__(self, stored):
        super().__init__("기록 복원 상태 확인 중 / Checking restored sync state")
        self.stored = stored


class PokeUnavailable(SupabaseError):
    """The server does not have the poke RPCs yet (migration not applied)."""

    def __init__(self):
        super().__init__(
            "서버에 찌르기 기능이 아직 없습니다. / Poke is not available on this server yet.",
            status=404,
        )


def _is_missing_rpc(error: SupabaseError) -> bool:
    """PostgREST answers 404 / PGRST202 when an RPC is not in its schema cache."""
    text = str(error).casefold()
    return (
        error.status == 404
        or "pgrst202" in text
        or "could not find the function" in text
    )


def load_or_create_device_id(path: Path, machine_marker: str | None = None) -> str:
    """Return an opaque installation ID that is not part of Anki profile sync.

    The marker is stored only as a hash. If an Anki data directory is copied to
    another computer, the copied installation ID is replaced instead of making
    both computers overwrite the same device row.
    """
    if machine_marker is None:
        raw_marker = f"{platform.node()}:{uuid.getnode()}"
        machine_marker = hashlib.sha256(raw_marker.encode("utf-8")).hexdigest()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        candidate = str(payload.get("device_id") or "")
        uuid.UUID(candidate)
        if payload.get("machine_marker") == machine_marker:
            return candidate
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        pass

    device_id = str(uuid.uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"device_id": device_id, "machine_marker": machine_marker}),
        encoding="utf-8",
    )
    temporary.replace(path)
    return device_id


def profile_device_id(installation_id: str, profile_path: Path) -> str:
    """Namespace one server device stream per local Anki profile."""
    namespace = uuid.UUID(installation_id)
    local_profile = str(profile_path.resolve()).casefold()
    return str(uuid.uuid5(namespace, local_profile))


class DeviceSyncLedger:
    """Maintain per-account monotonic snapshots for this installation."""

    def __init__(self, state: dict[str, Any]):
        self.state = state
        self.state.setdefault("accounts", {})

    def bind_device(self, device_id: str) -> None:
        """Reset copied per-device counters when this is a new installation."""
        previous = self.state.get("installation_id")
        if previous and previous != device_id:
            self.state.clear()
            self.state["accounts"] = {}
            self.state["baseline_on_next_activation"] = True
        self.state["installation_id"] = device_id

    def activate(
        self, user_id: str, day: str, active_seconds: int, answer_count: int
    ) -> None:
        active_user = self.state.get("active_user")
        active_day = self.state.get("active_day")
        if active_user == user_id and active_day == day:
            return
        if active_user and active_day == day:
            self._advance(active_user, day, active_seconds, answer_count)

        accounts = self.state["accounts"]
        first_account = not accounts
        baseline_existing = bool(self.state.get("baseline_on_next_activation"))
        include_existing = (first_account and not baseline_existing) or active_user == user_id
        target = accounts.setdefault(user_id, {}).setdefault(
            day,
            {
                "seconds_total": 0,
                "answers_total": 0,
                "segment_seconds": 0 if include_existing else int(active_seconds),
                "segment_answers": 0 if include_existing else int(answer_count),
                "revision": 0,
                "acknowledged_revision": 0,
            },
        )
        # Returning to an account begins a new segment at the current local
        # totals, so study performed while another account was active is excluded.
        if not include_existing and (active_user or active_day):
            target["segment_seconds"] = int(active_seconds)
            target["segment_answers"] = int(answer_count)
        self.state["active_user"] = user_id
        self.state["active_day"] = day
        self.state.pop("baseline_on_next_activation", None)

    def prepare(
        self,
        *,
        user_id: str,
        day: str,
        active_seconds: int,
        answer_count: int,
        time_goal_minutes: int,
        card_goal: int,
        status: str,
    ) -> dict[str, Any]:
        self.activate(user_id, day, active_seconds, answer_count)
        day_state = self._advance(user_id, day, active_seconds, answer_count)
        signature = [
            int(day_state["seconds_total"]),
            int(day_state["answers_total"]),
            int(time_goal_minutes),
            int(card_goal),
            status,
        ]
        if (
            day_state.get("signature") != signature
            or int(day_state.get("revision", 0))
            == int(day_state.get("acknowledged_revision", 0))
        ):
            day_state["revision"] = int(day_state.get("revision", 0)) + 1
            day_state["signature"] = signature
        return {
            "revision": int(day_state["revision"]),
            "active_seconds": signature[0],
            "answer_count": signature[1],
            "time_goal_minutes": signature[2],
            "card_goal": signature[3],
            "status": signature[4],
        }

    def acknowledge(self, user_id: str, day: str, revision: int) -> None:
        day_state = self.state.get("accounts", {}).get(user_id, {}).get(day)
        if day_state and int(day_state.get("revision", 0)) == int(revision):
            day_state["acknowledged_revision"] = int(revision)

    def rebase_from_server(
        self,
        *,
        user_id: str,
        day: str,
        active_seconds: int,
        answer_count: int,
        stored: dict[str, Any],
    ) -> dict[str, int]:
        """Resume a restored device stream without replaying old local totals.

        A restored profile can contain a ledger revision older than the row
        already stored for this device.  The server row is authoritative only
        when it is demonstrably at least as new and its counters have not gone
        backwards.  Current local totals become a new segment baseline: only
        study recorded after this call is added to the server totals.

        This deliberately does not guess which part of the restored local
        history was already uploaded.  Callers should surface the returned
        baseline values as a recovery notice rather than claiming a lossless
        merge.
        """
        def nonnegative_integer(value: Any, field: str) -> int:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"invalid server recovery {field}")
            if value < 0:
                raise ValueError(f"invalid server recovery {field}")
            return value

        server_revision = nonnegative_integer(stored.get("revision"), "revision")
        server_seconds = nonnegative_integer(
            stored.get("active_seconds"), "active_seconds"
        )
        server_answers = nonnegative_integer(
            stored.get("answer_count"), "answer_count"
        )
        local_seconds = nonnegative_integer(active_seconds, "local active_seconds")
        local_answers = nonnegative_integer(answer_count, "local answer_count")

        accounts = self.state["accounts"]
        day_state = accounts.get(user_id, {}).get(day, {})
        local_revision = nonnegative_integer(day_state.get("revision", 0), "local revision")
        known_seconds = nonnegative_integer(
            day_state.get("seconds_total", 0), "known active_seconds"
        )
        known_answers = nonnegative_integer(
            day_state.get("answers_total", 0), "known answer_count"
        )
        if server_revision < local_revision:
            raise ValueError("server recovery revision is older than the local ledger")
        if server_seconds < known_seconds or server_answers < known_answers:
            raise ValueError("server recovery counters are older than the local ledger")

        day_state = accounts.setdefault(user_id, {}).setdefault(day, {})
        day_state.clear()
        day_state.update(
            {
                "seconds_total": server_seconds,
                "answers_total": server_answers,
                "segment_seconds": local_seconds,
                "segment_answers": local_answers,
                "revision": server_revision,
                "acknowledged_revision": server_revision,
            }
        )
        self.state["active_user"] = user_id
        self.state["active_day"] = day
        self.state.pop("baseline_on_next_activation", None)
        return {
            "server_revision": server_revision,
            "server_active_seconds": server_seconds,
            "server_answer_count": server_answers,
            "baselined_local_seconds": local_seconds,
            "baselined_local_answers": local_answers,
        }

    def _advance(
        self, user_id: str, day: str, active_seconds: int, answer_count: int
    ) -> dict[str, Any]:
        day_state = self.state["accounts"].setdefault(user_id, {}).setdefault(
            day,
            {
                "seconds_total": 0,
                "answers_total": 0,
                "segment_seconds": int(active_seconds),
                "segment_answers": int(answer_count),
                "revision": 0,
                "acknowledged_revision": 0,
            },
        )
        seconds = int(active_seconds)
        answers = int(answer_count)
        day_state["seconds_total"] = int(day_state.get("seconds_total", 0)) + max(
            0, seconds - int(day_state.get("segment_seconds", seconds))
        )
        day_state["answers_total"] = int(day_state.get("answers_total", 0)) + max(
            0, answers - int(day_state.get("segment_answers", answers))
        )
        day_state["segment_seconds"] = seconds
        day_state["segment_answers"] = answers
        return day_state


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

    def bind_username(self, token: str, username: str, password: str) -> dict[str, Any]:
        return self._account_request(
            "bind", token=token, username=username, password=password
        )

    def sign_in_username(self, username: str, password: str) -> dict[str, Any]:
        return self._account_request("login", username=username, password=password)

    def recover_username(
        self, username: str, recovery_code: str, new_password: str
    ) -> dict[str, Any]:
        return self._account_request(
            "recover", username=username, recovery_code=recovery_code,
            password=new_password,
        )

    def _account_request(self, action: str, *, token: str | None = None, **fields: Any) -> dict[str, Any]:
        fields["username"] = str(fields.get("username", "")).strip().lower()
        result = self._request(
            "POST", "/functions/v1/account-auth", token=token or self.key,
            body={"action": action, **fields},
        )
        if (
            not isinstance(result, dict)
            or not result.get("access_token")
            or not result.get("refresh_token")
            or not isinstance(result.get("user"), dict)
            or not result["user"].get("id")
        ):
            raise SupabaseError("로그인 서버의 응답을 확인할 수 없습니다 / Invalid login response")
        return result

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

    def create_group(
        self,
        token: str,
        name: str,
        timezone_name: str = "Asia/Seoul",
        is_public: bool = False,
    ) -> dict[str, Any] | None:
        body: dict[str, Any] = {
            "group_name": name,
            "room_timezone": timezone_name,
        }
        if is_public:
            body["room_is_public"] = True
        result = self._request(
            "POST",
            "/rest/v1/rpc/create_study_group",
            token=token,
            body=body,
        )
        created = self._first(result)
        if not created:
            return None
        return {
            "id": created.get("id") or created.get("group_id"),
            "name": name,
            "invite_code": created.get("invite_code"),
            "time_zone": created.get("time_zone") or timezone_name,
            "day_start_hour": 4,
            "is_public": created.get("is_public", is_public),
        }

    def join_group(self, token: str, code: str) -> Any:
        try:
            result = self._request(
                "POST", "/rest/v1/rpc/join_study_group_safe",
                token=token, body={"code": code},
            )
        except SupabaseError as error:
            if error.status == 404:
                raise SupabaseError(
                    "서버 업데이트가 필요합니다. 잠시 후 다시 시도하세요. / "
                    "The room server must be updated before joining.", status=503
                ) from error
            if "study group is full" in str(error).casefold():
                raise SupabaseError(
                    "방 인원이 가득 찼습니다. 한 방에는 방장을 포함해 최대 8명까지 참여할 수 있습니다. / "
                    "This room is full. A room can have up to 8 people including the owner.",
                    status=409,
                ) from error
            raise
        if not isinstance(result, dict) or not result.get("ok"):
            if isinstance(result, dict) and result.get("error") == "TOO_MANY_ATTEMPTS":
                raise SupabaseError("초대 코드 시도가 너무 많습니다. 잠시 후 다시 시도하세요. / Too many attempts. Try again later.", status=429)
            raise SupabaseError("초대 코드를 확인해 주세요. / Check the invite code.", status=400)
        if not result.get("group_id"):
            raise SupabaseError("방 참여 결과를 확인할 수 없습니다. / Missing room confirmation.")
        return result["group_id"]

    def rotate_invite(self, token: str, group_id: str) -> str:
        result = self._request(
            "POST", "/rest/v1/rpc/rotate_study_group_invite", token=token,
            body={"target_group": group_id},
        )
        if not isinstance(result, str) or len(result) != 4:
            raise SupabaseError("초대 코드 재발급 결과를 확인할 수 없습니다. / Missing invite code.")
        return result

    def leave_group(self, token: str, group_id: str) -> Any:
        return self._request(
            "POST",
            "/rest/v1/rpc/leave_study_group",
            token=token,
            body={"target_group": group_id},
        )

    def list_group_members(
        self, token: str, group_id: str
    ) -> list[dict[str, Any]]:
        """Fetch the room roster directly from the server.

        This deliberately does not use the study-stat cache: moderation must
        act on the current membership, not on a previously rendered panel.
        """
        rows = self._request(
            "GET",
            "/rest/v1/group_members",
            token=token,
            query={
                "select": "user_id,joined_at",
                "group_id": f"eq.{group_id}",
                "order": "joined_at.asc",
            },
        ) or []
        user_ids = [str(row.get("user_id") or "") for row in rows]
        user_ids = [user_id for user_id in user_ids if user_id]
        profiles = []
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
        names = {
            str(row.get("id")): row.get("display_name")
            for row in profiles
            if row.get("id")
        }
        return [
            {
                "user_id": str(row.get("user_id")),
                "joined_at": row.get("joined_at"),
                "display_name": names.get(str(row.get("user_id"))),
            }
            for row in rows
            if row.get("user_id")
        ]

    def list_group_bans(self, token: str, group_id: str) -> list[dict[str, Any]]:
        result = self._request(
            "POST",
            "/rest/v1/rpc/list_study_group_bans",
            token=token,
            body={"target_group": group_id},
        ) or []
        if not isinstance(result, list):
            raise SupabaseError(
                "차단 목록 응답을 확인할 수 없습니다. / Invalid blocked-member response."
            )
        return [row for row in result if isinstance(row, dict) and row.get("user_id")]

    def moderate_group_member(
        self, token: str, group_id: str, user_id: str, *, blocked: bool
    ) -> Any:
        return self._request(
            "POST",
            "/rest/v1/rpc/moderate_study_group_member",
            token=token,
            body={
                "target_group": group_id,
                "target_user": user_id,
                "blocked": bool(blocked),
            },
        )

    def update_room_timezone(
        self, token: str, group_id: str, timezone_name: str
    ) -> Any:
        return self._request(
            "POST",
            "/rest/v1/rpc/update_room_timezone",
            token=token,
            body={
                "target_group": group_id,
                "new_timezone": str(timezone_name).strip(),
            },
        )

    def transfer_room_ownership(
        self, token: str, group_id: str, new_owner_id: str
    ) -> Any:
        return self._request(
            "POST",
            "/rest/v1/rpc/transfer_room_ownership",
            token=token,
            body={
                "target_group": group_id,
                "new_owner_id": str(new_owner_id).strip(),
            },
        )

    def kick_room_member(
        self, token: str, group_id: str, target_user: str
    ) -> Any:
        return self._request(
            "POST",
            "/rest/v1/rpc/kick_room_member",
            token=token,
            body={
                "target_group": group_id,
                "target_user": str(target_user).strip(),
            },
        )

    def cleanup_inactive_members(
        self, token: str, group_id: str, days: int = 14
    ) -> int:
        result = self._request(
            "POST",
            "/rest/v1/rpc/cleanup_inactive_members",
            token=token,
            body={
                "target_group": group_id,
                "days_inactive": int(days),
            },
        )
        if isinstance(result, int):
            return result
        try:
            return int(result)
        except (TypeError, ValueError):
            return 0

    def list_public_study_groups(
        self, token: str, timezone_name: str = "Asia/Seoul"
    ) -> list[dict[str, Any]]:
        result = self._request(
            "POST",
            "/rest/v1/rpc/list_public_study_groups",
            token=token,
            body={"user_timezone": str(timezone_name).strip()},
        )
        return result if isinstance(result, list) else []

    def poke_room_member(self, token: str, group_id: str, target_user: str) -> str | None:
        """Poke another member of the room; returns the server ``created_at``.

        Raises ``PokeUnavailable`` when the server migration is not applied,
        and a bilingual ``SupabaseError`` (status 429) for the rate limits.
        """
        try:
            result = self._request(
                "POST",
                "/rest/v1/rpc/poke_room_member",
                token=token,
                body={
                    "target_group": group_id,
                    "target_user": str(target_user).strip(),
                },
            )
        except SupabaseError as error:
            if _is_missing_rpc(error):
                raise PokeUnavailable() from error
            message = str(error).casefold()
            if "poke too soon" in message:
                raise SupabaseError(
                    "방금 찔렀어요. 1분 뒤에 다시 찌를 수 있어요. / "
                    "You just poked them. Try again in a minute.",
                    status=429,
                ) from error
            if "too many pokes" in message:
                raise SupabaseError(
                    "찌르기를 너무 많이 했어요. 잠시 후 다시 시도하세요. / "
                    "Too many pokes. Try again later.",
                    status=429,
                ) from error
            if "target is not in this group" in message:
                raise SupabaseError(
                    "이 친구는 지금 방에 없어요. / This person is no longer in the room.",
                    status=409,
                ) from error
            if "cannot poke yourself" in message:
                raise SupabaseError(
                    "자기 자신은 찌를 수 없어요. / You cannot poke yourself.",
                    status=400,
                ) from error
            raise
        created_at = self._first(result)
        return str(created_at) if created_at else None

    def fetch_my_pokes(
        self, token: str, group_id: str, since: str | None = None
    ) -> list[dict[str, Any]]:
        """Return unseen pokes to this account in the room, oldest first.

        The server marks the returned rows seen in the same call, so each poke
        is delivered at most once. Raises ``PokeUnavailable`` when the server
        migration is not applied.
        """
        body: dict[str, Any] = {"target_group": group_id}
        if since:
            body["since"] = since
        try:
            result = self._request(
                "POST", "/rest/v1/rpc/fetch_my_pokes", token=token, body=body,
            )
        except SupabaseError as error:
            if _is_missing_rpc(error):
                raise PokeUnavailable() from error
            raise
        if result is None:
            return []
        if not isinstance(result, list):
            raise SupabaseError("찌르기 응답을 확인할 수 없습니다. / Invalid poke response.")
        return [
            {
                "id": row.get("id"),
                "from_user": str(row["from_user"]),
                "created_at": row.get("created_at"),
            }
            for row in result
            if isinstance(row, dict) and row.get("from_user")
        ]

    def list_groups(self, token: str, user_id: str) -> list[dict[str, Any]]:
        rows = self._request(
            "GET",
            "/rest/v1/group_members",
            token=token,
            query={
                "select": (
                    "group_id,joined_at,"
                    "study_groups(id,name,invite_code,owner_id,created_at,time_zone,day_start_hour)"
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

    def record_device_day(
        self, token: str, *, group_id: str, device_id: str, study_day: str,
        revision: int, active_seconds: int, answer_count: int, status: str,
        time_goal_minutes: int | None = None, card_goal: int | None = None,
    ) -> dict[str, Any]:
        """Store one idempotent cumulative snapshot for this installation.

        Persist a revision before sending, reuse it for retries, and increase it
        for new snapshots (including presence changes). The server owns user ID
        and receipt time. Never send group/server totals as device counters.
        """
        body = {
            "target_group": group_id,
            "source_device": device_id,
            "target_day": study_day,
            "snapshot_revision": revision,
            "seconds_total": active_seconds,
            "answers_total": answer_count,
            "activity_status": status,
        }
        if time_goal_minutes is not None:
            body["time_goal"] = int(time_goal_minutes)
        if card_goal is not None:
            body["cards_goal"] = int(card_goal)
        try:
            result = self._request(
                "POST", "/rest/v1/rpc/record_device_day", token=token, body=body
            )
        except SupabaseError as error:
            if error.status == 404:
                raise SupabaseError(
                    "서버의 기기별 동기화 업데이트가 아직 적용되지 않았습니다.",
                    status=404,
                ) from error
            raise
        stored = self._first(result)
        if not isinstance(stored, dict) or not stored:
            raise SupabaseError("기기별 기록 저장 결과를 확인할 수 없습니다")
        if (stored.get("revision") != revision
                or stored.get("active_seconds") != active_seconds
                or stored.get("answer_count") != answer_count):
            for key, expected in (("group_id", group_id), ("device_id", device_id),
                                  ("study_day", study_day)):
                if stored.get(key) != expected:
                    raise SupabaseError("서버 기록 식별자가 일치하지 않습니다. / Snapshot identity mismatch.")
            raise DeviceSnapshotConflict(stored)
        return stored

    def sync_review_day(self, token: str, *, group_id: str, batch: dict) -> None:
        """Send native review identities, never add collection totals per PC."""
        result = self._request(
            "POST", "/rest/v1/rpc/sync_review_day", token=token,
            body={"target_group": group_id,
                  "source_collection": batch["source_collection"],
                  "target_day": batch["target_day"],
                  "reviews": batch["reviews"],
                  "removed_ids": batch["removed_ids"]},
        )
        receipt = self._first(result)
        if (not isinstance(receipt, dict)
                or receipt.get("study_day") != batch["target_day"]
                or not isinstance(receipt.get("active_review_count"), int)
                or not isinstance(receipt.get("active_time_ms"), int)):
            raise SupabaseError("복습 기록 저장 결과를 확인하지 못했습니다. / Review receipt missing.")

    def set_current_deck(self, token: str, group_id: str, device_id: str,
                         deck_name: str | None) -> None:
        self._request(
            "POST", "/rest/v1/rpc/set_current_deck", token=token,
            body={"target_group": group_id, "source_device": device_id,
                  "deck_name": deck_name[:300] if deck_name else None},
        )

    def fetch_group_day_stats(
        self, token: str, group_id: str, day: str
    ) -> list[dict[str, Any]]:
        """Read one finished room day's member totals for the weekly chart."""
        rows = self._request(
            "POST", "/rest/v1/rpc/get_group_device_stats", token=token,
            body={"target_group": group_id, "target_day": day},
        ) or []
        if not isinstance(rows, list):
            raise SupabaseError("방 기록 응답을 확인할 수 없습니다. / Invalid room stats response.")
        return [row for row in rows if isinstance(row, dict) and row.get("user_id")]

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
        try:
            stats = self._request(
                "POST",
                "/rest/v1/rpc/get_group_device_stats",
                token=token,
                query={
                    "group_id": f"eq.{group_id}",
                    "study_day": f"eq.{day}",
                },
                body={"target_group": group_id, "target_day": day},
            ) or []
        except SupabaseError as error:
            if error.status == 404:
                raise SupabaseError(
                    "서버의 기기별 동기화 업데이트가 아직 적용되지 않았습니다.",
                    status=404,
                ) from error
            raise
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

        try:
            decks = self._request(
                "POST", "/rest/v1/rpc/get_group_current_decks", token=token,
                body={"target_group": group_id},
            ) or []
        except SupabaseError as error:
            if error.status != 404:
                raise
            decks = []  # Older servers still show study totals.
        activity_error = False
        try:
            activity = self._request(
                "POST", "/rest/v1/rpc/get_group_activity_timeline", token=token,
                body={"target_group": group_id, "target_day": day},
            ) or []
        except SupabaseError as error:
            if error.status in (401, 403):
                raise
            activity = []
            if error.status != 404:
                activity_error = True
        decks_by_user = {row["user_id"]: row for row in decks}
        activity_by_user = {row["user_id"]: row for row in activity}
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
                "activity_known": False,
                "activity_buckets": [],
            }
            item.update(stats_by_user.get(user_id, {}))
            item.update(decks_by_user.get(user_id, {}))
            timeline = activity_by_user.get(user_id, {})
            if isinstance(timeline.get("activity_known"), bool):
                item["activity_known"] = timeline["activity_known"]
            if isinstance(timeline.get("activity_buckets"), list):
                item["activity_buckets"] = timeline["activity_buckets"]
            if activity_error:
                item["activity_error"] = True
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
