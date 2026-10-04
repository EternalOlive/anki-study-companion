import assert from "node:assert/strict";

const captured = { handler: null };
globalThis.Deno = {
  env: {
    get(name) {
      if (name === "SUPABASE_URL") return "https://project.test";
      if (name === "SUPABASE_SERVICE_ROLE_KEY") return "service-role-key";
      return undefined;
    },
  },
  serve(handler) {
    captured.handler = handler;
  },
};

await import("../supabase/functions/account-auth/index.ts");
assert.equal(typeof captured.handler, "function", "Deno.serve handler was not registered");

const USER_ID = "11111111-1111-4111-8111-111111111111";
const EXPECTED_CORS_HEADERS = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "authorization, x-client-info, apikey, content-type",
  "access-control-allow-methods": "POST, OPTIONS",
  "access-control-max-age": "86400",
};
const jsonResponse = (status, body) => new Response(JSON.stringify(body), {
  status,
  headers: { "content-type": "application/json" },
});

function request(body, headers = {}) {
  return new Request("https://function.test/account-auth", {
    method: "POST",
    headers: { "content-type": "application/json", ...headers },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

function methodRequest(method, headers = {}) {
  return new Request("https://function.test/account-auth", { method, headers });
}

async function invoke(body, { headers = {}, fetch } = {}) {
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    const call = { url: String(url), method: init.method || "GET", body: init.body, headers: init.headers };
    calls.push(call);
    return await fetch(call, calls);
  };
  const response = await captured.handler(request(body, headers));
  return { response, body: await response.json(), calls };
}

function rpcName(call) {
  return call.url.split("/rest/v1/rpc/")[1]?.split("?")[0] || "";
}

function allowedRateLimit(call) {
  if (rpcName(call) === "account_auth_rate_limit") {
    return jsonResponse(200, { allowed: true, retry_after: 0 });
  }
  return null;
}

function assertCorsHeaders(response) {
  for (const [name, value] of Object.entries(EXPECTED_CORS_HEADERS)) {
    assert.equal(response.headers.get(name), value, `${name} should be present`);
  }
}

async function testOptionsPreflightNeedsNoBodyAuthOrUpstreamCall() {
  let fetchCount = 0;
  globalThis.fetch = async () => {
    fetchCount += 1;
    return jsonResponse(500, {});
  };
  const response = await captured.handler(methodRequest("OPTIONS", {
    origin: "https://client.test",
    "access-control-request-method": "POST",
    "access-control-request-headers": "authorization, content-type",
  }));
  assert.equal(response.status, 204);
  assert.equal(await response.text(), "");
  assertCorsHeaders(response);
  assert.equal(fetchCount, 0);
}

async function testGetIsRejectedWithCorsHeaders() {
  let fetchCount = 0;
  globalThis.fetch = async () => {
    fetchCount += 1;
    return jsonResponse(500, {});
  };
  const response = await captured.handler(methodRequest("GET"));
  const body = await response.json();
  assert.equal(response.status, 405);
  assert.match(body.error, /Method not allowed/i);
  assertCorsHeaders(response);
  assert.equal(fetchCount, 0);
}

async function testUnauthenticatedBindRejected() {
  const result = await invoke(
    { action: "bind", username: "reader_7", password: "long-password" },
    { fetch: async (call) => allowedRateLimit(call) ?? jsonResponse(500, {}) },
  );
  assert.equal(result.response.status, 401);
  assert.match(result.body.error, /Authentication required/i);
  assert.equal(result.calls.some((call) => call.url.includes("/auth/v1/admin/users/")), false);
}

async function testActiveBindWrongPasswordNeverUpdatesAuthUser() {
  const result = await invoke(
    { action: "bind", username: "reader_7", password: "wrong-password" },
    {
      headers: { authorization: "Bearer caller-token" },
      fetch: async (call) => {
        const rate = allowedRateLimit(call);
        if (rate) return rate;
        if (call.url.endsWith("/auth/v1/user")) {
          return jsonResponse(200, { id: USER_ID, is_anonymous: false });
        }
        if (rpcName(call) === "account_auth_begin_bind") {
          return jsonResponse(200, { state: "active" });
        }
        if (call.url.includes("/auth/v1/token?grant_type=password")) {
          return jsonResponse(400, { error: "bad password" });
        }
        if (rpcName(call) === "account_auth_release_lease") return jsonResponse(200, {});
        return jsonResponse(500, {});
      },
    },
  );
  // A wrong current password is not an expired bearer session.
  assert.equal(result.response.status, 400);
  assert.match(result.body.error, /password is incorrect/i);
  assert.equal(result.calls.some((call) => call.url.includes("/auth/v1/admin/users/")), false);
}

async function testPendingNonAnonymousWrongPasswordNeverResetsAuthUser() {
  const result = await invoke(
    { action: "bind", username: "reader_7", password: "wrong-password" },
    {
      headers: { authorization: "Bearer caller-token" },
      fetch: async (call) => {
        const rate = allowedRateLimit(call);
        if (rate) return rate;
        if (call.url.endsWith("/auth/v1/user")) {
          return jsonResponse(200, { id: USER_ID, is_anonymous: false });
        }
        if (rpcName(call) === "account_auth_begin_bind") {
          return jsonResponse(200, { state: "pending" });
        }
        if (call.url.includes("/auth/v1/token?grant_type=password")) {
          return jsonResponse(400, { error: "bad password" });
        }
        if (rpcName(call) === "account_auth_release_lease") return jsonResponse(200, {});
        return jsonResponse(500, {});
      },
    },
  );
  assert.equal(result.response.status, 400);
  assert.equal(result.calls.some((call) => call.url.includes("/auth/v1/admin/users/")), false);
}

async function testRateLimitRunsBeforeCallerAuthentication() {
  let rateCalls = 0;
  const result = await invoke(
    { action: "bind", username: "reader_7", password: "long-password" },
    {
      headers: { authorization: "Bearer caller-token" },
      fetch: async (call) => {
        if (rpcName(call) === "account_auth_rate_limit") {
          rateCalls += 1;
          return jsonResponse(200, rateCalls === 1
            ? { allowed: false, retry_after: 31 }
            : { allowed: true, retry_after: 0 });
        }
        return jsonResponse(500, {});
      },
    },
  );
  assert.equal(result.response.status, 429);
  assert.equal(result.calls.some((call) => call.url.endsWith("/auth/v1/user")), false);
}

async function testMalformedAndOversizedBodies() {
  let fetchCount = 0;
  const fetch = async () => { fetchCount += 1; return jsonResponse(500, {}); };
  const malformed = await invoke("{not json", { fetch });
  assert.equal(malformed.response.status, 400);
  assertCorsHeaders(malformed.response);
  const oversized = await invoke("x".repeat(4097), { fetch });
  assert.equal(oversized.response.status, 413);
  assertCorsHeaders(oversized.response);
  assert.equal(fetchCount, 0);
}

async function testCorrectBindPreservesAuthenticatedUserId() {
  let signInAttempts = 0;
  const result = await invoke(
    { action: "bind", username: "Reader_7", password: "long-password" },
    {
      headers: { authorization: "Bearer caller-token" },
      fetch: async (call) => {
        const rate = allowedRateLimit(call);
        if (rate) return rate;
        if (call.url.endsWith("/auth/v1/user")) {
          return jsonResponse(200, { id: USER_ID, is_anonymous: true });
        }
        if (rpcName(call) === "account_auth_begin_bind") {
          const body = JSON.parse(call.body);
          assert.equal(body.target_user, USER_ID);
          assert.equal(body.target_username, "reader_7");
          return jsonResponse(200, { state: "pending" });
        }
        if (call.url.includes("/auth/v1/token?grant_type=password")) {
          signInAttempts += 1;
          return signInAttempts === 1
            ? jsonResponse(400, { error: "not configured yet" })
            : jsonResponse(200, {
                access_token: "access",
                refresh_token: "refresh",
                user: { id: USER_ID },
              });
        }
        if (call.url.includes(`/auth/v1/admin/users/${USER_ID}`)) {
          const body = JSON.parse(call.body);
          assert.equal(body.email, `${USER_ID}@users.invalid`);
          assert.equal(body.password, "long-password");
          return jsonResponse(200, { id: USER_ID });
        }
        if (rpcName(call) === "account_auth_finish_bind") {
          const body = JSON.parse(call.body);
          assert.equal(body.target_user, USER_ID);
          return jsonResponse(200, {});
        }
        return jsonResponse(500, {});
      },
    },
  );
  assert.equal(result.response.status, 200);
  assertCorsHeaders(result.response);
  assert.equal(result.body.user.id, USER_ID);
  assert.equal(result.body.username, "reader_7");
  assert.equal(typeof result.body.recovery_code, "string");
  assert.equal(result.calls.some((call) => call.url.includes(`/admin/users/`) && !call.url.endsWith(USER_ID)), false);
}

const tests = [
  testOptionsPreflightNeedsNoBodyAuthOrUpstreamCall,
  testGetIsRejectedWithCorsHeaders,
  testUnauthenticatedBindRejected,
  testActiveBindWrongPasswordNeverUpdatesAuthUser,
  testPendingNonAnonymousWrongPasswordNeverResetsAuthUser,
  testRateLimitRunsBeforeCallerAuthentication,
  testMalformedAndOversizedBodies,
  testCorrectBindPreservesAuthenticatedUserId,
];

for (const test of tests) {
  await test();
  console.log(`ok - ${test.name}`);
}
console.log(`${tests.length} account-auth runtime tests passed`);
