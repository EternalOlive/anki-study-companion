import io
import json
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

from study_companion.online import SupabaseClient, SupabaseError


class FakeResponse:
    def __init__(self, payload=None):
        self.raw = b"" if payload is None else json.dumps(payload).encode("utf-8")

    def read(self):
        return self.raw

    def close(self):
        pass


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return FakeResponse(response)


def body_of(request):
    return json.loads(request.data.decode("utf-8"))


def headers_of(request):
    return {key.lower(): value for key, value in request.header_items()}


class SupabaseClientTests(unittest.TestCase):
    def test_auth_requests_use_expected_endpoints_headers_and_payloads(self):
        opener = FakeOpener([
            {"user": {"id": "u1"}},
            {"access_token": "guest"},
            {"access_token": "a"},
            {"access_token": "b"},
            {"id": "u1", "is_anonymous": True},
            {"id": "u1", "email": "me@example.com"},
        ])
        client = SupabaseClient("https://example.supabase.co", "public-key", opener=opener)

        client.sign_up("me@example.com", "secret123")
        client.sign_in_anonymously()
        client.sign_in("me@example.com", "secret123")
        client.refresh("refresh-token")
        client.get_user("access")
        client.update_user("access", email="me@example.com")

        signup, anonymous, signin, refresh, get_user, update_user = [
            call[0] for call in opener.calls
        ]
        self.assertEqual(signup.full_url, "https://example.supabase.co/auth/v1/signup")
        self.assertEqual(body_of(signup), {"email": "me@example.com", "password": "secret123"})
        self.assertEqual(headers_of(signup)["apikey"], "public-key")
        self.assertEqual(signup.get_method(), "POST")
        self.assertEqual(anonymous.full_url, "https://example.supabase.co/auth/v1/signup")
        self.assertEqual(body_of(anonymous), {"data": {}})
        self.assertEqual(parse_qs(urlparse(signin.full_url).query), {"grant_type": ["password"]})
        self.assertEqual(body_of(signin)["email"], "me@example.com")
        self.assertEqual(parse_qs(urlparse(refresh.full_url).query), {"grant_type": ["refresh_token"]})
        self.assertEqual(body_of(refresh), {"refresh_token": "refresh-token"})
        self.assertEqual(get_user.get_method(), "GET")
        self.assertEqual(headers_of(get_user)["authorization"], "Bearer access")
        self.assertEqual(update_user.get_method(), "PUT")
        self.assertEqual(body_of(update_user), {"email": "me@example.com"})

    def test_profile_group_and_stats_writes_are_authenticated_upserts(self):
        opener = FakeOpener([
            [{"id": "u1", "display_name": "윤"}],
            [{"group_id": "g1", "invite_code": "ABC"}],
            "g1",
            None,
            [{"group_id": "g1", "user_id": "u1"}],
        ])
        client = SupabaseClient(opener=opener)

        profile = client.upsert_profile("access", "u1", "윤")
        group = client.create_group("access", "친구들")
        joined = client.join_group("access", " abc ")
        client.leave_group("access", "g1")
        stats = client.upsert_daily_stats("access", [{"group_id": "g1", "user_id": "u1"}])

        self.assertEqual(profile["display_name"], "윤")
        self.assertEqual(group["invite_code"], "ABC")
        self.assertEqual(group["id"], "g1")
        self.assertEqual(group["name"], "친구들")
        self.assertEqual(joined, "g1")
        self.assertEqual(len(stats), 1)
        requests = [call[0] for call in opener.calls]
        for request in requests:
            self.assertEqual(headers_of(request)["authorization"], "Bearer access")
        self.assertEqual(body_of(requests[0]), {"id": "u1", "display_name": "윤"})
        self.assertIn("resolution=merge-duplicates", headers_of(requests[0])["prefer"])
        self.assertEqual(body_of(requests[1]), {"group_name": "친구들"})
        self.assertEqual(body_of(requests[2]), {"code": " abc "})
        self.assertEqual(body_of(requests[3]), {"target_group": "g1"})
        self.assertTrue(requests[3].full_url.endswith("/rest/v1/rpc/leave_study_group"))
        stats_query = parse_qs(urlparse(requests[4].full_url).query)
        self.assertEqual(stats_query["on_conflict"], ["group_id,user_id,study_day"])

    def test_list_groups_flattens_embedded_group(self):
        opener = FakeOpener([[
            {
                "group_id": "g1",
                "joined_at": "2026-09-30T01:00:00Z",
                "study_groups": {"id": "g1", "name": "친구들", "invite_code": "CODE"},
            }
        ]])
        client = SupabaseClient(opener=opener)

        groups = client.list_groups("access", "u1")

        self.assertEqual(groups, [{
            "id": "g1", "name": "친구들", "invite_code": "CODE",
            "joined_at": "2026-09-30T01:00:00Z",
        }])
        query = parse_qs(urlparse(opener.calls[0][0].full_url).query)
        self.assertEqual(query["user_id"], ["eq.u1"])
        self.assertIn("study_groups", query["select"][0])

    def test_fetch_group_today_merges_profiles_with_separate_request(self):
        opener = FakeOpener([
            [{"user_id": "u1"}, {"user_id": "u2"}],
            [
                {"group_id": "g1", "user_id": "u1", "study_day": "2026-09-30", "answer_count": 10},
                {"group_id": "g1", "user_id": "u2", "study_day": "2026-09-30", "answer_count": 7},
            ],
            [{"id": "u1", "display_name": "윤"}, {"id": "u2", "display_name": "친구"}],
        ])
        client = SupabaseClient(opener=opener)

        rows = client.fetch_group_today("access", "g1", "2026-09-30")

        self.assertEqual([row["display_name"] for row in rows], ["윤", "친구"])
        membership_query = parse_qs(urlparse(opener.calls[0][0].full_url).query)
        stats_query = parse_qs(urlparse(opener.calls[1][0].full_url).query)
        profile_query = parse_qs(urlparse(opener.calls[2][0].full_url).query)
        self.assertEqual(membership_query["group_id"], ["eq.g1"])
        self.assertEqual(stats_query["group_id"], ["eq.g1"])
        self.assertEqual(stats_query["study_day"], ["eq.2026-09-30"])
        self.assertEqual(profile_query["id"], ["in.(u1,u2)"])

    def test_fetch_with_no_stats_returns_group_members_with_zeroes(self):
        opener = FakeOpener([
            [{"user_id": "u1"}],
            [],
            [{"id": "u1", "display_name": "윤"}],
        ])
        rows = SupabaseClient(opener=opener).fetch_group_today("access", "g1", "2026-09-30")
        self.assertEqual(rows[0]["display_name"], "윤")
        self.assertEqual(rows[0]["answer_count"], 0)
        self.assertEqual(rows[0]["status"], "stopped")
        self.assertEqual(len(opener.calls), 3)

    def test_http_error_exposes_server_message_without_credentials(self):
        error = HTTPError(
            "https://example.supabase.co/auth/v1/token",
            400,
            "Bad Request",
            {},
            io.BytesIO(json.dumps({"message": "Invalid login credentials"}).encode()),
        )
        client = SupabaseClient(opener=FakeOpener([error]))

        with self.assertRaisesRegex(SupabaseError, "Invalid login credentials") as raised:
            client.sign_in("me@example.com", "wrong-password")

        self.assertEqual(raised.exception.status, 400)
        self.assertNotIn("wrong-password", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
