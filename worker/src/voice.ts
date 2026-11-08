/**
 * Voice I/O via Groq, using the same GROQ_API_KEY as Chief's model calls.
 *
 * STT:    POST /audio/transcriptions  -> Groq Whisper (whisper-large-v3-turbo)
 * TTS:    POST /audio/speech          -> Groq PlayAI TTS (playai-tts, voice Fritz)
 *
 * Both run server-side at the edge so the browser never needs the API key and
 * works identically on desktop and mobile (Web Speech API is spotty on iOS).
 */
import type { Env } from "./env";
import { ProviderError } from "./chat/types";

const GROQ_BASE = "https://api.groq.com/openai/v1";

const STT_MODEL = "whisper-large-v3-turbo";
const TTS_MODEL = "canopylabs/orpheus-arabic-saudi";
const TTS_VOICE = "Fritz";
const TTS_RESPONSE_FORMAT = "wav";

function groqHeaders(apiKey: string): HeadersInit {
  return {
    authorization: `Bearer ${apiKey}`,
  };
}

export async function speechToText(env: Env, audioBlob: Blob): Promise<string> {
  const apiKey = env.GROQ_API_KEY;
  if (!apiKey) throw new ProviderError("Groq API key is not configured on this Worker", 503, false);

  const form = new FormData();
  form.append("file", audioBlob, "audio.webm");
  form.append("model", STT_MODEL);
  form.append("language", "en");
  form.append("temperature", "0");

  const response = await fetch(`${GROQ_BASE}/audio/transcriptions`, {
    method: "POST",
    headers: groqHeaders(apiKey),
    body: form,
  });

  if (!response.ok) {
    let detail = "";
    try { detail = await response.text(); } catch { /* ignore */ }
    throw new ProviderError(`Groq STT failed: ${response.status}${detail ? `: ${detail}` : ""}`, response.status, response.status >= 500 || response.status === 429);
  }

  const payload = (await response.json()) as Record<string, unknown>;
  const text = typeof payload.text === "string" ? payload.text : "";
  if (!text.trim()) throw new ProviderError("Groq STT returned no text", 502, true);
  return text;
}

export async function textToSpeech(env: Env, text: string): Promise<{ audioBase64: string; contentType: string }> {
  const apiKey = env.GROQ_API_KEY;
  if (!apiKey) throw new ProviderError("Groq API key is not configured on this Worker", 503, false);

  const body = {
    model: TTS_MODEL,
    input: text,
    voice: TTS_VOICE,
    response_format: TTS_RESPONSE_FORMAT,
  };

  const response = await fetch(`${GROQ_BASE}/audio/speech`, {
    method: "POST",
    headers: {
      ...groqHeaders(apiKey),
      "content-type": "application/json",
    },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    let detail = "";
    try { detail = await response.text(); } catch { /* ignore */ }
    throw new ProviderError(`Groq TTS failed: ${response.status}${detail ? `: ${detail}` : ""}`, response.status, response.status >= 500 || response.status === 429);
  }

  const contentType = response.headers.get("content-type") ?? "audio/wav";
  const bytes = await response.arrayBuffer();
  const base64 = btoa(String.fromCharCode(...new Uint8Array(bytes)));
  return { audioBase64: base64, contentType };
}
