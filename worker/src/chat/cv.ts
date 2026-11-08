/**
 * Computer-vision via Groq's Qwen2-VL model, using the same GROQ_API_KEY as
 * Chief's model calls.  When a user attaches an image, this sends it to the
 * vision model for a plain-language description that gets folded into the
 * chat context before Chief sees it.
 */
import type { Env } from "../env";
import { ProviderError } from "./types";

const GROQ_BASE = "https://api.groq.com/openai/v1";

function groqHeaders(apiKey: string): HeadersInit {
  return {
    authorization: `Bearer ${apiKey}`,
    "content-type": "application/json",
  };
}

/**
 * Describe an image (data URL or base64) in plain language.  Returns a short
 * caption suitable for inclusion in a chat message.
 */
export async function describeImage(
  env: Env,
  imageDataUrl: string,
): Promise<string> {
  const apiKey = env.GROQ_API_KEY;
  if (!apiKey) throw new ProviderError("Groq API key is not configured on this Worker", 503, false);
  const cvModel = (env.CV_MODEL ?? "").trim();
  if (!cvModel) throw new ProviderError("CV model is not configured. Set CV_MODEL env var (e.g. a Groq vision model).", 503, false);

  const body = {
    model: cvModel,
    messages: [
      {
        role: "user",
        content: [
          { type: "text", text: "Describe this image in plain language. Focus on what is visible and relevant: text, objects, people, UI elements, charts, errors, or anything a user would want to know about it. Keep it concise but complete." },
          { type: "image_url", image_url: { url: imageDataUrl } },
        ],
      },
    ],
    temperature: 0,
    max_completion_tokens: 1024,
  };

  const response = await fetch(`${GROQ_BASE}/chat/completions`, {
    method: "POST",
    headers: groqHeaders(apiKey),
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    let detail = "";
    try { detail = await response.text(); } catch { /* ignore */ }
    throw new ProviderError(`Groq CV failed: ${response.status}${detail ? `: ${detail}` : ""}`, response.status, response.status >= 500 || response.status === 429);
  }

  const payload = (await response.json()) as Record<string, unknown>;
  const choices = Array.isArray(payload.choices) ? payload.choices : [];
  const first = (choices[0] ?? {}) as Record<string, unknown>;
  const message = (first.message ?? {}) as Record<string, unknown>;
  const content = typeof message.content === "string" ? message.content : "";
  if (!content.trim()) throw new ProviderError("Groq CV returned no description", 502, true);
  return content;
}
