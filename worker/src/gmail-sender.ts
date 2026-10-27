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
 */
import type { Env } from "../env";

export class GmailSenderError extends Error {}

const GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token";
const GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send";
const GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send";

let cachedAccess: { token: string; expiresAt: number } | null = null;

/** Obtain a fresh access token for the central Gmail sender account. */
async function refreshAccessToken(env: Env): Promise<string> {
  const now = Date.now();
  if (cachedAccess && cachedAccess.expiresAt > now + 60_000) {
    return cachedAccess.token;
  }

  const refreshToken = env.GMAIL_SENDER_REFRESH_TOKEN;
  const senderEmail = env.GMAIL_SENDER_EMAIL;
  if (!refreshToken || !senderEmail) {
    throw new GmailSenderError("central Gmail sender is not configured");
  }

  const body = new URLSearchParams({
    client_id: env.GOOGLE_CLIENT_ID,
    client_secret: env.GOOGLE_CLIENT_SECRET,
    refresh_token: refreshToken,
    grant_type: "refresh_token",
  });

  const response = await fetch(GMAIL_TOKEN_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      Accept: "application/json",
    },
    body: body.toString(),
  });

  if (response.status !== 200) {
    const text = await response.text().catch(() => "");
    throw new GmailSenderError(`Gmail token refresh failed with HTTP ${response.status}${text ? ": " + text.slice(0, 200) : ""}`);
  }

  const payload = (await response.json()) as { access_token?: string; expires_in?: number };
  const token = payload.access_token;
  if (!token) {
    throw new GmailSenderError("Gmail token refresh returned no access_token");
  }

  const expiresIn = Number(payload.expires_in) || 3600;
  cachedAccess = { token, expiresAt: now + expiresIn * 1000 };
  return token;
}

/** Build an RFC 822 MIME message and return its URL-safe base64raw encoding. */
function buildRaw(sender: string, to: string, subject: string, body: string): string {
  const msg = [
    `From: ${sender}`,
    `To: ${to}`,
    `Subject: ${subject}`,
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
  const accessToken = await refreshAccessToken(env);
  const sender = env.GMAIL_SENDER_EMAIL!;
  const raw = buildRaw(sender, recipient, subject, body);

  const response = await fetch(GMAIL_SEND_URL, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ raw }),
  });

  if (response.status >= 500) {
    throw new GmailSenderError(`Gmail API error ${response.status}; the email was not sent`);
  }
  if (response.status >= 400) {
    throw new GmailSenderError(`Gmail API rejected the message ${response.status}; the email was not sent`);
  }

  const payload = (await response.json().catch(() => ({}))) as { id?: string };
  const messageId = payload.id;
  if (!messageId) {
    throw new GmailSenderError("Gmail API accepted the request but returned no message id");
  }
  return messageId;
}
