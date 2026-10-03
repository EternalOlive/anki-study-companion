const MAX_BODY_BYTES = 4096;
const USERNAME_RE = /^[a-z0-9_]{4,24}$/;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const RECOVERY_RE = /^[A-Za-z0-9_-]{43}$/;

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")?.replace(/\/$/, "") ?? "";
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";

class PublicError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

class UpstreamError extends Error {
  status: number;
  payload: unknown;

  constructor(status: number, payload: unknown) {
    super(`upstream request failed (${status})`);
    this.status = status;
    this.payload = payload;
  }
}

function json(status: number, body: Record<string, unknown>): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function normalizeUsername(value: unknown): string {
  const username = typeof value === "string" ? value.trim().toLowerCase() : "";
  if (!USERNAME_RE.test(username)) {
    throw new PublicError(400, "Username must be 4-24 lowercase letters, numbers, or underscores.");
  }
  return username;
}

function validatePassword(value: unknown): string {
  if (typeof value !== "string") throw new PublicError(400, "Invalid password.");
  const characters = Array.from(value).length;
  const bytes = new TextEncoder().encode(value).byteLength;
  if (characters < 10 || bytes > 72) {
    throw new PublicError(400, "Password must be at least 10 characters and at most 72 UTF-8 bytes.");
  }
  return value;
}

function syntheticEmail(userId: string): string {
  if (!UUID_RE.test(userId)) throw new Error("invalid user id returned by auth");
  return `${userId.toLowerCase()}@users.invalid`;
}

function randomRecoveryCode(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  const binary = Array.from(bytes, (value) => String.fromCharCode(value)).join("");
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function hmacHex(value: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(SERVICE_KEY),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(value));
  return Array.from(new Uint8Array(signature), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function upstream(
  path: string,
  init: RequestInit,
  authorization = `Bearer ${SERVICE_KEY}`,
): Promise<unknown> {
  const response = await fetch(`${SUPABASE_URL}${path}`, {
    ...init,
    signal: AbortSignal.timeout(10_000),
    headers: {
      apikey: SERVICE_KEY,
      authorization,
      "content-type": "application/json",
      ...(init.headers ?? {}),
    },
  });
  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = null; }
  }
  if (!response.ok) throw new UpstreamError(response.status, payload);
  return payload;
}

async function readBoundedBody(request: Request): Promise<string> {
  if (!request.body) return "";
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > MAX_BODY_BYTES) {
      await reader.cancel();
      throw new PublicError(413, "Request body is too large.");
    }
    chunks.push(value);
  }
  const merged = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    merged.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(merged);
  } catch {
    throw new PublicError(400, "Request body must be UTF-8.");
  }
}

async function rpc(name: string, body: Record<string, unknown>): Promise<any> {
  return await upstream(`/rest/v1/rpc/${name}`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

async function getCaller(request: Request): Promise<Record<string, unknown>> {
  const authorization = request.headers.get("authorization") ?? "";
  if (!authorization.toLowerCase().startsWith("bearer ")) {
    throw new PublicError(401, "Authentication required.");
  }
  try {
    const user = await upstream("/auth/v1/user", { method: "GET" }, authorization);
    if (typeof user !== "object" || user === null || Array.isArray(user)) throw new Error();
    return user as Record<string, unknown>;
  } catch (error) {
    if (error instanceof UpstreamError && (error.status === 401 || error.status === 403)) {
      throw new PublicError(401, "Authentication required.");
    }
    throw error;
  }
}

async function signIn(userId: string, password: string): Promise<Record<string, unknown>> {
  const session = await upstream("/auth/v1/token?grant_type=password", {
    method: "POST",
    body: JSON.stringify({ email: syntheticEmail(userId), password }),
  });
  if (typeof session !== "object" || session === null || Array.isArray(session)) {
    throw new Error("invalid auth session");
  }
  const result = session as Record<string, unknown>;
  const user = result.user as Record<string, unknown> | undefined;
  if (!result.access_token || !result.refresh_token || user?.id !== userId) {
    throw new Error("auth session identity mismatch");
  }
  return result;
}

async function updateAuthUser(userId: string, attributes: Record<string, unknown>): Promise<void> {
  await upstream(`/auth/v1/admin/users/${userId}`, {
    method: "PUT",
    body: JSON.stringify(attributes),
  });
}

async function releaseLease(username: string, userId: string, claimToken: string): Promise<void> {
  try {
    await rpc("account_auth_release_lease", {
      target_username: username,
      target_user: userId,
      claim_token: claimToken,
    });
  } catch {
    // A short database lease also guarantees eventual recovery if cleanup fails.
  }
}

async function enforceRateLimit(request: Request, action: string, username: string): Promise<void> {
  const forwarded = request.headers.get("x-forwarded-for")?.split(",", 1)[0]?.trim();
  const address = request.headers.get("cf-connecting-ip")?.trim() || forwarded || "unknown";
  const ipHash = await hmacHex(`ip:${address}`);
  const usernameHash = await hmacHex(`username:${username}`);
  const limits: Record<string, [number, number]> = {
    bind: [10, 600],
    login: [10, 600],
    recover: [5, 600],
  };
  const selectedLimit = limits[action];
  if (!selectedLimit) throw new Error("invalid rate-limit action");
  const [maximumAttempts, windowSeconds] = selectedLimit;
  const ipResult = await rpc("account_auth_rate_limit", {
    target_scope: "ip",
    target_subject_hash: ipHash,
    target_operation: "all",
    maximum_attempts: 60,
    window_seconds: 600,
  });
  const usernameResult = await rpc("account_auth_rate_limit", {
    target_scope: "username",
    target_subject_hash: usernameHash,
    target_operation: action,
    maximum_attempts: maximumAttempts,
    window_seconds: windowSeconds,
  });
  if (ipResult.allowed !== true || usernameResult.allowed !== true) {
    const retry = Math.max(
      Number(ipResult.retry_after) || 0,
      Number(usernameResult.retry_after) || 0,
      1,
    );
    throw new PublicError(429, `Too many attempts. Try again in ${retry} seconds.`);
  }
}

function databasePublicError(error: UpstreamError): PublicError | null {
  const payload = error.payload as Record<string, unknown> | null;
  const message = typeof payload?.message === "string" ? payload.message : "";
  if (message.includes("USERNAME_UNAVAILABLE")) return new PublicError(409, "Username is unavailable.");
  if (message.includes("USER_ALREADY_LINKED")) return new PublicError(409, "This account is already linked.");
  if (message.includes("BIND_REQUIRES_GUEST")) return new PublicError(409, "This account is already linked.");
  if (message.includes("ACCOUNT_BUSY") || message.includes("LEASE_LOST")) {
    return new PublicError(409, "Account update is in progress. Try again shortly.");
  }
  if (message.includes("INVALID_LOGIN")) return new PublicError(401, "Invalid username or password.");
  if (message.includes("INVALID_RECOVERY")) return new PublicError(401, "Invalid recovery information.");
  return null;
}

async function bind(
  request: Request,
  username: string,
  password: string,
): Promise<Record<string, unknown>> {
  const caller = await getCaller(request);
  const userId = typeof caller.id === "string" ? caller.id : "";
  if (!UUID_RE.test(userId)) throw new PublicError(401, "Authentication required.");
  const callerIsAnonymous = caller.is_anonymous === true;
  const claimToken = crypto.randomUUID();
  const account = await rpc("account_auth_begin_bind", {
    target_username: username,
    target_user: userId,
    caller_is_anonymous: callerIsAnonymous,
    claim_token: claimToken,
  });

  try {
    let session: Record<string, unknown>;
    if (account.state === "active") {
      try {
        session = await signIn(userId, password);
      } catch (error) {
        if (error instanceof UpstreamError && (error.status === 400 || error.status === 401)) {
          throw new PublicError(400, "Current password is incorrect.");
        }
        throw error;
      }
    } else if (account.state === "pending") {
      try {
        session = await signIn(userId, password);
      } catch (error) {
        if (!(error instanceof UpstreamError) || (error.status !== 400 && error.status !== 401)) throw error;
        if (!callerIsAnonymous) {
          throw new PublicError(400, "Current password is incorrect.");
        }
        await updateAuthUser(userId, {
          email: syntheticEmail(userId),
          password,
          email_confirm: true,
        });
        session = await signIn(userId, password);
      }
    } else {
      throw new Error("invalid account state");
    }

    const recoveryCode = randomRecoveryCode();
    await rpc("account_auth_finish_bind", {
      target_username: username,
      target_user: userId,
      claim_token: claimToken,
      recovery_digest: await sha256Hex(recoveryCode),
    });
    return { ...session, username, recovery_code: recoveryCode };
  } catch (error) {
    await releaseLease(username, userId, claimToken);
    throw error;
  }
}

async function login(username: string, password: string): Promise<Record<string, unknown>> {
  const claimToken = crypto.randomUUID();
  const account = await rpc("account_auth_begin_login", {
    target_username: username,
    claim_token: claimToken,
  });
  const userId = typeof account.user_id === "string" ? account.user_id : "";
  if (!UUID_RE.test(userId)) throw new Error("invalid account user");

  try {
    let session: Record<string, unknown>;
    try {
      session = await signIn(userId, password);
    } catch (error) {
      if (error instanceof UpstreamError && (error.status === 400 || error.status === 401)) {
        throw new PublicError(401, "Invalid username or password.");
      }
      throw error;
    }
    if (account.state !== "pending") return { ...session, username };

    const recoveryCode = randomRecoveryCode();
    await rpc("account_auth_finish_bind", {
      target_username: username,
      target_user: userId,
      claim_token: claimToken,
      recovery_digest: await sha256Hex(recoveryCode),
    });
    return { ...session, username, recovery_code: recoveryCode };
  } catch (error) {
    if (account.state === "pending") await releaseLease(username, userId, claimToken);
    throw error;
  }
}

async function recover(
  username: string,
  password: string,
  recoveryCode: unknown,
): Promise<Record<string, unknown>> {
  if (typeof recoveryCode !== "string" || !RECOVERY_RE.test(recoveryCode)) {
    throw new PublicError(401, "Invalid recovery information.");
  }
  const claimToken = crypto.randomUUID();
  const account = await rpc("account_auth_begin_recovery", {
    target_username: username,
    recovery_digest: await sha256Hex(recoveryCode),
    claim_token: claimToken,
  });
  const userId = typeof account.user_id === "string" ? account.user_id : "";
  if (!UUID_RE.test(userId)) throw new Error("invalid account user");

  try {
    await updateAuthUser(userId, { password });
    const session = await signIn(userId, password);
    const nextRecoveryCode = randomRecoveryCode();
    await rpc("account_auth_finish_recovery", {
      target_username: username,
      target_user: userId,
      claim_token: claimToken,
      recovery_digest: await sha256Hex(nextRecoveryCode),
    });
    return { ...session, username, recovery_code: nextRecoveryCode };
  } catch (error) {
    await releaseLease(username, userId, claimToken);
    throw error;
  }
}

Deno.serve(async (request: Request): Promise<Response> => {
  try {
    if (!SUPABASE_URL || !SERVICE_KEY) throw new Error("missing server configuration");
    if (request.method !== "POST") throw new PublicError(405, "Method not allowed.");
    const declaredLength = Number(request.headers.get("content-length") || "0");
    if (Number.isFinite(declaredLength) && declaredLength > MAX_BODY_BYTES) {
      throw new PublicError(413, "Request body is too large.");
    }
    const raw = await readBoundedBody(request);
    let body: Record<string, unknown>;
    try {
      const parsed = JSON.parse(raw);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error();
      body = parsed as Record<string, unknown>;
    } catch {
      throw new PublicError(400, "Invalid JSON request.");
    }

    const action = body.action;
    if (action !== "bind" && action !== "login" && action !== "recover") {
      throw new PublicError(400, "Invalid action.");
    }
    const username = normalizeUsername(body.username);
    const password = validatePassword(body.password);
    await enforceRateLimit(request, action, username);

    const result = action === "bind"
      ? await bind(request, username, password)
      : action === "login"
      ? await login(username, password)
      : await recover(username, password, body.recovery_code);
    return json(200, result);
  } catch (error) {
    if (error instanceof PublicError) return json(error.status, { error: error.message });
    if (error instanceof UpstreamError) {
      const safe = databasePublicError(error);
      if (safe) return json(safe.status, { error: safe.message });
    }
    return json(500, { error: "Account service is temporarily unavailable." });
  }
});
