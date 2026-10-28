/**
 * Runs INSIDE the suno.com tab: the side panel injects it with
 * chrome.scripting.executeScript. That way every request uses the session the
 * page itself is signed in with and goes out from the page's own origin, with
 * its cookies, exactly like Suno's web player - no credentials are stored in
 * the extension.
 *
 * Chrome serializes only this function's source, so it must stay
 * self-contained: no imports, no outer variables.
 *
 * request:
 *   { kind: 'session' }                    -> { userId, email, handle }
 *   { kind: 'api', method, path, body? }   -> { ok, status, data, token }
 *
 * `token` is the access token that was sent as the Bearer header. A license
 * key is wrapped under SHA-256 of it, so the caller needs it to unwrap the key.
 *
 * Never rejects: a rejected promise does not reach the caller across
 * executeScript, so failures come back as { error }.
 */
export async function sunoTabCall(request) {
  // The web app talks to its API through the same-origin /a proxy; the
  // studio-api host is the older direct address, tried if /a does not serve it.
  const API_BASES = [`${location.origin}/a`, 'https://studio-api.prod.suno.com'];
  // Suno's auth server (a Clerk frontend API), and the same-origin proxy of it.
  const AUTH_BASES = ['https://auth.suno.com', `${location.origin}/api/clerk`];
  const REFRESH_MARGIN_SECONDS = 60;

  function cookieValues(name) {
    return document.cookie
      .split(';')
      .map((part) => part.trim())
      .filter((part) => part.startsWith(`${name}=`))
      .map((part) => {
        const value = part.slice(name.length + 1);
        try {
          return decodeURIComponent(value);
        } catch {
          return value;
        }
      });
  }

  function claimsOf(token) {
    const parts = token.split('.');
    if (parts.length !== 3) return null;
    try {
      const base64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
      const padded = base64.padEnd(base64.length + ((4 - (base64.length % 4)) % 4), '=');
      const bytes = Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
      return JSON.parse(new TextDecoder().decode(bytes));
    } catch {
      return null;
    }
  }

  // The page keeps its session JWT in the readable __session cookie. Visitor
  // (signed-out) tokens carry no user id; of the signed-in ones, take the
  // newest, even if expired, since its session id is what mints a fresh one.
  function signedInSession() {
    let best = null;
    for (const token of cookieValues('__session')) {
      const claims = claimsOf(token);
      if (!claims || claims['suno.com/claims/visitor'] === true) continue;
      if (typeof claims['suno.com/claims/user_id'] !== 'string') continue;
      if (!best || (claims.exp ?? 0) > (best.claims.exp ?? 0)) best = { token, claims };
    }
    return best;
  }

  async function mintToken(sessionId) {
    if (!sessionId) return null;
    for (const base of AUTH_BASES) {
      try {
        const response = await fetch(`${base}/v1/client/sessions/${encodeURIComponent(sessionId)}/tokens`, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
          body: '',
        });
        if (!response.ok) continue;
        const data = await response.json().catch(() => null);
        if (typeof data?.jwt === 'string' && data.jwt) return data.jwt;
      } catch {
        // try the next auth address
      }
    }
    return null;
  }

  // Same id the page sends as Device-Id (its analytics anonymous id).
  function deviceId() {
    let raw = null;
    try {
      raw = localStorage.getItem('ajs_anonymous_id');
    } catch {
      // storage blocked
    }
    raw = raw || cookieValues('ajs_anonymous_id')[0] || '';
    if (raw.startsWith('"') && raw.endsWith('"')) raw = raw.slice(1, -1);
    return raw.replace(/[^a-zA-Z0-9-]/g, '') || null;
  }

  async function callApi(method, path, body, token) {
    const headers = {
      Authorization: `Bearer ${token}`,
      'Browser-Token': JSON.stringify({ token: btoa(JSON.stringify({ timestamp: Date.now() })) }),
    };
    const device = deviceId();
    if (device) headers['Device-Id'] = device;
    if (body !== undefined) headers['Content-Type'] = 'application/json';

    let result = { ok: false, status: 0, data: null };
    for (const base of API_BASES) {
      let response;
      try {
        response = await fetch(base + path, {
          method,
          headers,
          body: body === undefined ? undefined : JSON.stringify(body),
          credentials: base.startsWith(location.origin) ? 'same-origin' : 'omit',
        });
      } catch {
        continue; // network or CORS failure; try the next address
      }
      const text = await response.text();
      let data = null;
      try {
        data = JSON.parse(text);
      } catch {
        // not JSON (e.g. an HTML error page)
      }
      result = { ok: response.ok, status: response.status, data };
      if (response.status !== 404) break; // 404: this address does not serve the path
    }
    return result;
  }

  try {
    const session = signedInSession();
    if (!session) {
      return { error: 'You are not signed in on this suno.com tab. Sign in there, then press Connect.' };
    }
    const sessionId = session.claims.sid;
    let token = session.token;
    if ((session.claims.exp ?? 0) - Date.now() / 1000 < REFRESH_MARGIN_SECONDS) {
      token = (await mintToken(sessionId)) ?? token;
    }

    if (request.kind === 'session') {
      const claims = claimsOf(token) ?? session.claims;
      return {
        userId: claims['suno.com/claims/user_id'],
        email: claims['suno.com/claims/email'] ?? null,
        handle: claims['suno/handle'] ?? null,
      };
    }

    if (request.kind === 'api') {
      let result = await callApi(request.method, request.path, request.body, token);
      if (result.status === 401) {
        const minted = await mintToken(sessionId);
        if (minted && minted !== token) {
          token = minted;
          result = await callApi(request.method, request.path, request.body, token);
        }
      }
      return { ...result, token };
    }

    return { error: `Unknown request kind: ${request.kind}` };
  } catch (error) {
    return { error: String(error?.message ?? error) };
  }
}
