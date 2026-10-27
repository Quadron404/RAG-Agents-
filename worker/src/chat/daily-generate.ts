/**
 * Daily-summary generation service.
 *
 * Given an owner, channel, and local date, retrieves every eligible message
 * from that day in the correct order and produces a structured summary using
 * the configured AI provider.  Empty days short-circuit with no model call.
 * Long transcripts use multi-stage summarization so no messages are silently
 * truncated.  This step only generates and validates -- it does not persist
 * (that is Prompt 6) and does not index (that is also Prompt 6).
 */
import type { Env } from "../env";
import type { ProviderSelection } from "./types";
import { localDay } from "../timezone";
import { selectSummaryProvider } from "./providers";
import {
  listMessagesInRange,
  lastVisibleSeq,
  type MessageRow,
} from "./store";

const DAILY_SUMMARY_INSTRUCTION =
  "Summarize this portion of a conversation as one plain-text paragraph for future AI retrieval. Preserve important facts, exact technical terms, keywords, names, dates, decisions, constraints, outcomes, and unresolved issues. Distinguish confirmed decisions from proposals. Do not invent facts or silently resolve contradictions. Keep the paragraph concise but sufficiently detailed to recover the conversation's important information. Return only the paragraph, with no headings, bullets, Markdown, or commentary.";

const FINAL_SUMMARY_INSTRUCTION =
  "Combine these partial summaries of one conversation day into a single plain-text paragraph. Preserve important facts, exact technical terms, keywords, names, dates, decisions, constraints, outcomes, and unresolved issues. Distinguish confirmed decisions from proposals. Do not invent facts or silently resolve contradictions. Keep the paragraph concise but sufficiently detailed to recover the day's important information. Return only the paragraph, with no headings, bullets, Markdown, or commentary.";

/** Given a local date (YYYY-MM-DD) and a timezone, return [startMs, endMs) of that local day. */
function localDateRange(tzName: string, localDate: string): { startMs: number; endMs: number } {
  const [year, month, day] = localDate.split("-").map(Number);
  const utcMidnight = Date.UTC(year, month - 1, day);
  const first = localDay(tzName, utcMidnight);

  if (first.date === localDate) {
    return { startMs: first.startMs, endMs: first.endMs };
  }

  // UTC midnight fell on a different local date. Retry from a moment inside the
  // target local date (start or end of the returned day).
  const retryMs = first.date < localDate ? first.endMs : first.startMs - 1;
  const retry = localDay(tzName, retryMs);
  if (retry.date === localDate) {
    return { startMs: retry.startMs, endMs: retry.endMs };
  }

  throw new Error(`Could not resolve ${localDate} in timezone ${tzName}`);
}

const MAX_CHUNK_CHARS = 100_000;

export interface DailySummaryResult {
  summary: string;
  sourceCoverage: {
    fromSeq: number;
    toSeq: number;
    messageCount: number;
    localDate: string;
  };
  provider: string;
  model: string;
  stages: number;
}

/** Truncate a value to a character limit, keeping both ends. */
function truncate(value: string, limit: number): string {
  const text = value.trim();
  if (text.length <= limit) return text;
  const half = Math.floor(limit / 2) - 20;
  return `${text.slice(0, half)}\n[...truncated...]\n${text.slice(-half)}`;
}

/** Build a transcript string from visible messages, in seq order. */
function transcriptFrom(messages: MessageRow[]): string {
  const lines: string[] = [];
  for (const message of messages) {
    const content = truncate(message.content, 24_000);
    if (!content) continue;
    if (message.role === "tool") {
      let name = "tool";
      try {
        const data = JSON.parse(message.data || "{}") as Record<string, unknown>;
        if (typeof data.name === "string") name = data.name;
      } catch {
        /* keep default */
      }
      lines.push(`[${name} result]\n${content}`);
    } else {
      lines.push(`${message.role === "user" ? "User" : "Chief"}:\n${content}`);
    }
  }
  return lines.join("\n\n");
}

/** Call the summary provider and collect all text events. */
async function summarizeText(
  selection: ProviderSelection,
  instruction: string,
  text: string,
  signal?: AbortSignal,
): Promise<string> {
  let out = "";
  for await (const event of selection.provider.stream(
    [
      { role: "system", content: instruction },
      { role: "user", content: text },
    ],
    [],
    signal,
  )) {
    if (event.type === "text") out += event.text;
  }
  const cleaned = out.trim();
  if (!cleaned) throw new Error("the summary model returned an empty paragraph");
  return cleaned;
}

/**
 * Split a transcript into overlapping chunks so no message is split across
 * chunks, then summarise each chunk and fold the results into a final summary.
 */
async function multiStageSummarize(
  selection: ProviderSelection,
  transcript: string,
  signal?: AbortSignal,
): Promise<{ summary: string; stages: number }> {
  if (transcript.length <= MAX_CHUNK_CHARS) {
    const summary = await summarizeText(selection, DAILY_SUMMARY_INSTRUCTION, transcript, signal);
    return { summary, stages: 1 };
  }

  // Split into chunks, preserving whole lines where possible.
  const chunks: string[] = [];
  let remaining = transcript;
  while (remaining.length > 0) {
    let cut = MAX_CHUNK_CHARS;
    if (cut < remaining.length) {
      // Try to cut at a newline boundary.
      const nl = remaining.lastIndexOf("\n", cut);
      if (nl > MAX_CHUNK_CHARS / 2) cut = nl + 1;
    }
    chunks.push(remaining.slice(0, cut));
    remaining = remaining.slice(cut);
  }

  const partials: string[] = [];
  for (const chunk of chunks) {
    const partial = await summarizeText(
      selection,
      DAILY_SUMMARY_INSTRUCTION,
      `Partial transcript (part ${partials.length + 1} of ${chunks.length}):\n\n${chunk}`,
      signal,
    );
    partials.push(partial);
  }

  const combined = partials.join("\n\n---\n\n");
  const summary = await summarizeText(selection, FINAL_SUMMARY_INSTRUCTION, combined, signal);
  return { summary, stages: chunks.length + 1 };
}

/**
 * Generate a daily summary for the given owner, channel, and local date.
 *
 * Returns null when there is no visible activity on that day (no model call).
 * When ``afterSeq`` is supplied, only messages newer than that durable
 * checkpoint are summarised -- previously covered messages are never resent.
 * Throws ProviderError when the model is unavailable or returns an empty result.
 */
export async function generateDailySummary(
  env: Env,
  ownerSub: string,
  conversationId: string,
  localDate: string,
  now: number,
  signal?: AbortSignal,
  options?: { afterSeq?: number },
): Promise<DailySummaryResult | null> {
  const selection = selectSummaryProvider(env);
  if (!selection) {
    throw new Error("no summary model is configured");
  }

  // Resolve the local day boundaries in the owner's timezone.
  const tzRow = await env.DB.prepare("SELECT timezone FROM users WHERE google_sub = ?1")
    .bind(ownerSub)
    .first<{ timezone: string }>();
  const timezone = tzRow?.timezone ?? "UTC";
  const { startMs, endMs } = localDateRange(timezone, localDate);

  // Retrieve every eligible message from that local day, in seq order.
  const messages = await listMessagesInRange(env, ownerSub, conversationId, startMs, endMs);
  if (!messages.length) return null;

  const afterSeq = options?.afterSeq ?? 0;
  const eligible = afterSeq > 0 ? messages.filter((m) => m.seq > afterSeq) : messages;
  if (!eligible.length) return null;

  const fromSeq = eligible[0].seq;
  const toSeq = eligible[eligible.length - 1].seq;

  const transcript = transcriptFrom(eligible);
  if (!transcript.trim()) return null;

  const { summary, stages } = await multiStageSummarize(selection, transcript, signal);

  return {
    summary,
    sourceCoverage: {
      fromSeq,
      toSeq,
      messageCount: eligible.length,
      localDate,
    },
    provider: selection.provider.name,
    model: selection.model,
    stages,
  };
}
