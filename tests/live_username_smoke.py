"""Opt-in real-server check using a new, isolated test account (never a user profile).

Run with --live only after deploying account-auth and its database migration.
The test room is left in finally; the random test Auth account remains for an
operator to audit. No passwords, sessions or recovery codes are logged/saved.
"""
from datetime import date
from pathlib import Path
import secrets
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from study_companion.online import SupabaseClient, SupabaseError


def rejected(operation):
    try:
        operation()
    except SupabaseError as error:
        assert error.status in (400, 401, 403), error.status
        return error.status
    else:
        raise AssertionError("Invalid credentials were accepted")


def main():
    if sys.argv[1:] != ["--live"]:
        raise SystemExit("Explicit --live is required; this creates an isolated server test account.")
    client = SupabaseClient(timeout=30)
    username = "verify_" + secrets.token_hex(6)
    password = secrets.token_urlsafe(24)
    new_password = secrets.token_urlsafe(24)
    session = client.sign_in_anonymously()
    user_id = session["user"]["id"]
    group = None
    print("Test account:", username, flush=True)
    try:
        client.upsert_profile(session["access_token"], user_id, "Auth verification")
        group = client.create_group(session["access_token"], "Login verification")
        session = client.bind_username(session["access_token"], username, password)
        assert session["user"]["id"] == user_id
        assert session["username"] == username
        assert session["recovery_code"]
        assert not session["user"].get("is_anonymous", False)
        print("PASS guest identity preserved", flush=True)

        other_pc = SupabaseClient(timeout=30)
        other_session = other_pc.sign_in_username(username.upper(), password)
        assert other_session["user"]["id"] == user_id
        rooms = other_pc.list_groups(other_session["access_token"], user_id)
        assert any(room["id"] == group["id"] for room in rooms)
        print("PASS independent client login and room recovery", flush=True)

        token = other_session["access_token"]
        payload = dict(group_id=group["id"], study_day=date.today().isoformat(),
                       revision=1, status="paused", time_goal_minutes=60, card_goal=100)
        first = dict(payload, device_id=str(uuid.uuid4()), active_seconds=120, answer_count=8)
        second = dict(payload, device_id=str(uuid.uuid4()), active_seconds=60, answer_count=4)
        client.record_device_day(token, **first)
        other_pc.record_device_day(token, **second)
        client.record_device_day(token, **first)
        rows = client.fetch_group_today(token, group["id"], payload["study_day"])
        row = next(row for row in rows if row["user_id"] == user_id)
        assert row["active_seconds"] == 180 and row["answer_count"] == 12, row
        print("PASS two device totals and replay idempotency", flush=True)

        rejected(lambda: client.sign_in_username(username, "wrongpassword123"))
        assert rejected(lambda: client.bind_username(token, username, "wrongpassword123")) == 400
        rotated = client.bind_username(token, username, password)
        code = rotated["recovery_code"]
        assert code != session["recovery_code"]
        rejected(lambda: client.recover_username(username, "0" * 64, new_password))
        session = client.recover_username(username, code, new_password)
        assert session["user"]["id"] == user_id and session["recovery_code"] != code
        rejected(lambda: client.recover_username(username, code, password))
        rejected(lambda: client.sign_in_username(username, password))
        session = client.sign_in_username(username, new_password)
        refreshed = client.refresh(session["refresh_token"])
        session = refreshed
        assert client.get_user(session["access_token"])["id"] == user_id
        print("PASS password/recovery rotation and refresh", flush=True)
    finally:
        if group:
            client.leave_group(session["access_token"], group["id"])
            print("Test room removed by leaving; isolated test account retained.", flush=True)


if __name__ == "__main__":
    main()
