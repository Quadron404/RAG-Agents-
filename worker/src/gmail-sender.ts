/**
 * Worker-native Gmail sender: the central bot mailbox.
 *
 * Uses GMAIL_SENDER_EMAIL and GMAIL_SENDER_REFRESH_TOKEN (Cloudflare Worker
 * secrets) to obtain an access token and send email through the Gmail API.
 * This is the single sender for all notification and routine emails -- users
 * never connect their personal Gmail just to receive notifications.
 *
 * The recipient is always the authenticated user's verified Google email,
 * resolved server-side from the session.  The model cannot supply a recipient.
 *
 * Sender identity:
 *  * When GMAIL_SENDER_ALIAS is set, it must be a verified send-as alias of the
 *    central Gmail account (checked against Gmail's sendAs settings before use).
 *    It is never silently replaced by the primary address.
 *  * The From header always resolves to the verified alias (when configured) or
 *    the configured central sender address -- never to a model-supplied value.
 */
import type { Env } from "./env";

export class GmailSenderError extends Error {
  /** True when the failure is permanent (e.g. invalid_grant) and retrying cannot help. */
  permanent: boolean;
  constructor(message: string, permanent = false) {
    super(message);
    this.name = "GmailSenderError";
    this.permanent = permanent;
  }
}

const GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token";
const GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send";
const GMAIL_SENDAS_URL = "https://gmail.googleapis.com/gmail/v1/users/me/settings/sendAs";
const GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send";

//: Ceilings so a single message cannot be used to exfiltrate or flood.
const MAX_SUBJECT_CHARS = 200;
const MAX_BODY_CHARS = 100_000;
//: CR/LF in a header value is header injection; neutralise it.
const HEADER_CRLF = /[\r\n]+/g;

let cachedAccess: { token: string; expiresAt: number } | null = null;
//: Cached outcome of verifying a configured alias, so the sendAs check is not
//: repeated on every send and a permanent failure is not retried indefinitely.
let verifiedAlias: string | null = null;
let aliasFailure: Error | null = null;

// Exported for test isolation only; not part of the production sender API.
export function __resetSenderCacheForTests(): void {
  cachedAccess = null;
  verifiedAlias = null;
  aliasFailure = null;
}

function nowMs(): number {
  return Date.now();
}

/** Strip CR/LF from a header value so it cannot inject new headers. */
function sanitizeHeader(value: string): string {
  return value.replace(HEADER_CRLF, " ");
}

/** Obtain a fresh access token for the central Gmail sender account. */
async function refreshAccessToken(env: Env): Promise<string> {
  const now = nowMs();
  if (cachedAccess && cachedAccess.expiresAt > now + 60_000) {
    return cachedAccess.token;
  }

  const refreshToken = env.GMAIL_SENDER_REFRESH_TOKEN;
  const senderEmail = env.GMAIL_SENDER_EMAIL;
  if (!refreshToken || !senderEmail) {
    throw new GmailSenderError("central Gmail sender is not configured", true);
  }

  const body = new URLSearchParams({
    client_id: env.GOOGLE_CLIENT_ID,
    client_secret: env.GOOGLE_CLIENT_SECRET,
    refresh_token: refreshToken,
    grant_type: "refresh_token",
  });

  let response: Response;
  try {
    response = await fetch(GMAIL_TOKEN_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        Accept: "application/json",
      },
      body: body.toString(),
    });
  } catch (error) {
    throw new GmailSenderError(`Gmail token refresh could not reach Google: ${(error as Error).message}`, true);
  }

  if (response.status !== 200) {
    const text = await response.text().catch(() => "");
    let permanent = false;
    try {
      const payload = JSON.parse(text);
      if (payload && typeof payload === "object" && (payload as { error?: string }).error === "invalid_grant") {
        permanent = true;
      }
    } catch {
      /* not JSON; treat as transient */
    }
    throw new GmailSenderError(
      `Gmail token refresh failed with HTTP ${response.status}${text ? ": " + text.slice(0, 200) : ""}`,
      permanent,
    );
  }

  const payload = (await response.json()) as { access_token?: string; expires_in?: number };
  const token = payload.access_token;
  if (!token) {
    throw new GmailSenderError("Gmail token refresh returned no access_token", true);
  }

  const expiresIn = Number(payload.expires_in) || 3600;
  cachedAccess = { token, expiresAt: now + expiresIn * 1000 };
  return token;
}

/** Verify that a configured alias is a verified send-as identity of the central account. */
async function verifyAlias(env: Env, alias: string): Promise<string> {
  if (verifiedAlias !== null) return verifiedAlias;
  if (aliasFailure) throw aliasFailure;

  const accessToken = await refreshAccessToken(env);
  let response: Response;
  try {
    response = await fetch(GMAIL_SENDAS_URL, {
      method: "GET",
      headers: {
        Authorization: `Bearer ${accessToken}`,
        Accept: "application/json",
      },
    });
  } catch (error) {
    aliasFailure = new GmailSenderError(
      `could not verify Gmail send-as alias: ${(error as Error).message}`,
      true,
    );
    throw aliasFailure;
  }

  if (response.status !== 200) {
    const text = await response.text().catch(() => "");
    aliasFailure = new GmailSenderError(
      `Gmail send-as lookup failed with HTTP ${response.status}${text ? ": " + text.slice(0, 200) : ""}`,
      true,
    );
    throw aliasFailure;
  }

  const payload = (await response.json().catch(() => ({}))) as { sendAs?: { sendAsEmail?: string; verificationStatus?: string }[] };
  const sendAs = Array.isArray(payload.sendAs) ? payload.sendAs : [];
  const entry = sendAs.find((e) => e && e.sendAsEmail === alias);
  if (!entry || entry.verificationStatus !== "accepted") {
    aliasFailure = new GmailSenderError(
      `Gmail send-as alias ${alias} is not configured or not verified`,
      true,
    );
    throw aliasFailure;
  }

  verifiedAlias = alias;
  return alias;
}

/** Resolve the From address: the verified alias when configured, else the central sender. */
async function resolveSender(env: Env): Promise<string> {
  const alias = env.GMAIL_SENDER_ALIAS;
  if (alias && alias.trim()) {
    return verifyAlias(env, alias.trim());
  }
  const senderEmail = env.GMAIL_SENDER_EMAIL;
  if (!senderEmail) {
    throw new GmailSenderError("central Gmail sender is not configured", true);
  }
  return senderEmail;
}

/** Build an RFC 822 MIME message and return its URL-safe base64raw encoding. */
function buildRaw(sender: string, to: string, subject: string, body: string): string {
  const msg = [
    `From: ${sanitizeHeader(sender)}`,
    `To: ${sanitizeHeader(to)}`,
    `Subject: ${sanitizeHeader(subject)}`,
    `MIME-Version: 1.0`,
    `Content-Type: text/plain; charset="utf-8"`,
    "",
    body,
  ].join("\r\n");

  const encoded = btoa(unescape(encodeURIComponent(msg)));
  return encoded.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/**
 * Send an email through the central Gmail sender.
 *
 * @param recipient - the authenticated user's verified Google email (never from the model)
 * @param subject - email subject
 * @param body - email body (plain text)
 * @returns the Gmail message ID on success
 */
export async function sendGmail(
  env: Env,
  recipient: string,
  subject: string,
  body: string,
): Promise<string> {
  if (!recipient || typeof recipient !== "string") {
    throw new GmailSenderError("recipient is required", true);
  }
  if (typeof subject !== "string" || subject.length > MAX_SUBJECT_CHARS) {
    throw new GmailSenderError(`subject is required and must be at most ${MAX_SUBJECT_CHARS} characters`, true);
  }
  if (typeof body !== "string" || body.length > MAX_BODY_CHARS) {
    throw new GmailSenderError(`body must be at most ${MAX_BODY_CHARS} characters`, true);
  }

  const accessToken = await refreshAccessToken(env);
  const sender = await resolveSender(env);
  const raw = buildRaw(sender, recipient, subject, body);

  let response: Response;
  try {
    response = await fetch(GMAIL_SEND_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${accessToken}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ raw }),
    });
  } catch (error) {
    throw new GmailSenderError(`Gmail API could not be reached: ${(error as Error).message}`, true);
  }

  if (response.status >= 500) {
    throw new GmailSenderError(`Gmail API error ${response.status}; the email was not sent`, true);
  }
  if (response.status >= 400) {
    const text = await response.text().catch(() => "");
    // invalid_grant on the send path means the sender grant is gone permanently.
    let permanent = false;
    try {
      const payload = JSON.parse(text);
      if (payload && typeof payload === "object" && (payload as { error?: string }).error === "invalid_grant") {
        permanent = true;
      }
    } catch {
      /* not JSON */
    }
    throw new GmailSenderError(`Gmail API rejected the message ${response.status}; the email was not sent`, permanent);
  }

  const payload = (await response.json().catch(() => ({}))) as { id?: string };
  const messageId = payload.id;
  if (!messageId) {
    throw new GmailSenderError("Gmail API accepted the request but returned no message id", true);
  }
  return messageId;
}
