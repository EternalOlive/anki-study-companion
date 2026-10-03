import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
FUNCTION = ROOT / "supabase" / "functions" / "account-auth" / "index.ts"
MIGRATION = ROOT / "supabase" / "migrations" / "20261003_username_accounts.sql"


class AccountAuthServerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.function = FUNCTION.read_text(encoding="utf-8")
        cls.migration = MIGRATION.read_text(encoding="utf-8")

    def test_edge_function_has_bounded_credentials_and_no_external_dependency(self):
        self.assertIn("const MAX_BODY_BYTES = 4096", self.function)
        self.assertIn("characters < 10 || bytes > 72", self.function)
        self.assertIn("/^[a-z0-9_]{4,24}$/", self.function)
        self.assertNotIn("https://deno.land", self.function)
        self.assertNotIn("npm:", self.function)

    def test_edge_function_uses_admin_api_without_mutating_auth_schema(self):
        self.assertIn("/auth/v1/admin/users/${userId}", self.function)
        self.assertIn("@users.invalid", self.function)
        self.assertNotRegex(self.migration.lower(), r"\b(insert|update|delete)\s+(into\s+)?auth\.users")

    def test_recovery_is_random_hashed_and_rotated(self):
        self.assertIn("crypto.getRandomValues(new Uint8Array(32))", self.function)
        self.assertGreaterEqual(self.function.count("await sha256Hex("), 3)
        self.assertIn("recovery_hash = decode(recovery_digest, 'hex')", self.migration)
        self.assertNotRegex(self.migration, r"(?i)recovery_(code|token)\s+text")

    def test_private_tables_and_functions_are_not_client_accessible(self):
        for table in ("account_credentials", "account_auth_rate_limits"):
            self.assertIn(f"alter table public.{table} enable row level security", self.migration)
            self.assertIn(f"revoke all on public.{table} from public, anon, authenticated", self.migration)
        self.assertNotIn("to anon;", self.migration)
        self.assertNotIn("to authenticated;", self.migration)
        self.assertGreaterEqual(self.migration.count("to service_role;"), 7)

    def test_rate_limit_identity_is_hmac_and_credentials_are_not_logged(self):
        self.assertIn('name: "HMAC"', self.function)
        self.assertIn('request.headers.get("x-forwarded-for")', self.function)
        self.assertIn('target_scope: "ip"', self.function)
        self.assertIn('target_scope: "username"', self.function)
        self.assertIn('await hmacHex(`username:${username}`)', self.function)
        self.assertNotRegex(self.function, r"console\.(log|debug|info|warn|error)")

    def test_account_changes_use_short_atomic_leases(self):
        self.assertGreaterEqual(self.migration.count("interval '2 minutes'"), 3)
        self.assertIn("and lease_token = claim_token", self.migration)
        self.assertIn("and lease_expires_at > clock_timestamp()", self.migration)
        self.assertIn("account_auth_release_lease", self.function)

    def test_network_and_request_body_work_are_bounded(self):
        self.assertIn("AbortSignal.timeout(10_000)", self.function)
        self.assertIn("request.body.getReader()", self.function)
        self.assertIn("total > MAX_BODY_BYTES", self.function)
        self.assertNotIn("request.text()", self.function)

    def test_nonanonymous_pending_retry_cannot_overwrite_password(self):
        pending_branch = self.function.split('account.state === "pending"', 1)[1]
        self.assertIn("if (!callerIsAnonymous)", pending_branch)
        self.assertLess(
            pending_branch.index("if (!callerIsAnonymous)"),
            pending_branch.index("await updateAuthUser"),
        )


if __name__ == "__main__":
    unittest.main()
