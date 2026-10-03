import json
import unittest

from study_companion.online import SupabaseClient, SupabaseError


class Response:
    def __init__(self, data):
        self.data = data

    def read(self):
        return json.dumps(self.data).encode()

    def close(self):
        pass


class UsernameAuthTests(unittest.TestCase):
    def test_username_operations_use_server_function_and_do_not_expose_internal_email(self):
        calls = []
        session = {"access_token": "access", "refresh_token": "refresh", "user": {"id": "u"}}
        def opener(request, timeout):
            calls.append(request)
            return Response(session)
        client = SupabaseClient("https://example.test", "public-key", opener=opener)
        self.assertEqual(client.bind_username("guest-token", " TEST_User ", "password123"), session)
        self.assertEqual(client.sign_in_username("TEST_User", "password123"), session)
        self.assertEqual(client.recover_username("test_user", "recovery", "password234"), session)
        for request, action in zip(calls, ("bind", "login", "recover")):
            self.assertEqual(request.full_url, "https://example.test/functions/v1/account-auth")
            self.assertEqual(request.get_method(), "POST")
            body = json.loads(request.data)
            self.assertEqual(body["action"], action)
            self.assertEqual(body["username"], "test_user")
            self.assertNotIn("email", body)
        self.assertEqual(calls[0].get_header("Authorization"), "Bearer guest-token")
        self.assertEqual(calls[1].get_header("Authorization"), "Bearer public-key")

    def test_incomplete_response_never_counts_as_login(self):
        for result in (None, {}, {"user": {"id": "u"}}, {"access_token": "a", "refresh_token": "r", "user": {}}):
            with self.subTest(result=result):
                client = SupabaseClient(opener=lambda *args, **kwargs: Response(result))
                with self.assertRaises(SupabaseError):
                    client.sign_in_username("user", "password123")


if __name__ == "__main__":
    unittest.main()
