/**
 * Incremental history generation for a single channel.
 *
 * Only messages newer than the channel's durable checkpoint are ever submitted
 * to the summarisation model.  A source-message cutoff is captured when the job
 * begins, so messages that arrive while the job runs stay unprocessed.  The
 * summary is persisted to D1 before the checkpoint is advanced, and the
 * channel's canonical RAG document is rebuilt once the summary is saved.
 */
import type { Env } from "../env";
import { localDay } from "../timezone";
import { selectSummaryProvider } from "./providers";
import {
  getConversationById,
  listMessagesInSeqRange,
} from "./store";
import { applyDailySummary } from "./daily-publish";

const INCREMENTAL_HISTORY_INSTRUCTION =
  "You are summarising a portion of a conversation history. Create one coherent, plain-text " +
  "history summary that preserves exact important keywords, names, identifiers, dates, " +
  "configurations, decisions, chronology, user preferences, completed work and unresolved " +
  "issues. Distinguish confirmed decisions from proposals. Do not invent facts or silently " +
  "resolve contradictions. Return only the paragraph, with no headings, bullets, Markdown, " +
  "or commentary.";

const MAX_TRANSCRIPT_CHARS = 120_000;
const MAX_MESSAGE_CHARS = 24_000;

export interface IncrementalHistoryResult {
  summary: string;
  fromSeq: number;
  toSeq: number;
  messageCount: number;
  localDate: string;
  published: boolean;
  indexed: boolean;
  indexSkipped: boolean;
  indexError: string | null;
  sourceMessages: { role: string; content: string; seq: number }[];
}

function truncate(value: string, limit: number): string {
  const text = value.trim();
  if (text.length <= limit) return text;
  const half = Math.floor(limit / 2) - 20;
  return `${text.slice(0, half)}\n[...truncated...]\n${text.slice(-half)}`;
}

function transcriptFrom(messages: { role: string; content: string }[]): string {
  const lines: string[] = [];
  for (const message of messages) {
    const content = truncate(message.content, MAX_MESSAGE_CHARS);
    if (!content) continue;
    lines.push(`${message.role === "user" ? "User" : "Chief"}:\n${content}`);
  }
  return truncate(lines.join("\n\n"), MAX_TRANSCRIPT_CHARS);
}

/** Call the summary provider with a custom instruction and collect all text events. */
export async function summarizeText(
  env: Env,
  instruction: string,
  transcript: string,
): Promise<string> {
  const selection = selectSummaryProvider(env);
  if (!selection) {
    throw new Error("no summary model is configured");
  }
  let text = "";
  for await (const event of selection.provider.stream(
    [
      { role: "system", content: instruction },
      { role: "user", content: transcript },
    ],
    [],
  )) {
    if (event.type === "text") text += event.text;
  }
  const cleaned = text.trim();
  if (!cleaned) throw new Error("the summary model returned an empty paragraph");
  return cleaned;
}

/** Call the summary provider with the incremental-history instruction. */
export async function summarizeWithProvider(env: Env, transcript: string): Promise<string> {
  return summarizeText(env, INCREMENTAL_HISTORY_INSTRUCTION, `Conversation transcript follows. Summarise these messages into a history summary.\n\n${transcript}`);
}

/**
 * Generate history for every unprocessed message up to ``cutoffSeq`` and persist it.
 *
 * Returns null when there is nothing new to summarise.  The checkpoint is only
 * advanced after the summary has been saved to D1.
 */
export async function generateIncrementalHistory(
  env: Env,
  ownerSub: string,
  conversationId: string,
  cutoffSeq: number,
  now: number,
): Promise<IncrementalHistoryResult | null> {
  const conversation = await getConversationById(env, conversationId);
  if (!conversation) throw new Error("conversation not found");

  const summarizedThrough = conversation.summarized_through;
  if (cutoffSeq <= summarizedThrough) {
    return null;
  }

  const messages = await listMessagesInSeqRange(
    env,
    ownerSub,
    conversationId,
    summarizedThrough + 1,
    cutoffSeq,
  );
  if (!messages.length) {
    return null;
  }

  const fromSeq = messages[0].seq;
  const toSeq = messages[messages.length - 1].seq;

  const transcript = transcriptFrom(
    messages.map((m) => ({ role: m.role, content: m.content })),
  );
  if (!transcript.trim()) {
    return null;
  }

  const summary = await summarizeWithProvider(env, transcript);

  const tzRow = await env.DB.prepare("SELECT timezone FROM users WHERE google_sub = ?1")
    .bind(ownerSub)
    .first<{ timezone: string }>();
  const timezone = tzRow?.timezone ?? "UTC";
  const { date: localDate } = localDay(timezone, now);

  const applied = await applyDailySummary(
    env,
    ownerSub,
    conversationId,
    localDate,
    summary,
    fromSeq,
    toSeq,
    now,
  );

  if (!applied.saved) {
    // Nothing new was persisted (not applied, or only skipped with no new coverage).
    // Checkpoint stays put; messages remain eligible for a legitimate retry.
    return {
      summary: applied.content,
      fromSeq,
      toSeq,
      messageCount: messages.length,
      localDate,
      published: false,
      indexed: false,
      indexSkipped: false,
      indexError: null,
      sourceMessages: messages.map((m) => ({ role: m.role, content: m.content, seq: m.seq })),
    };
  }

  return {
    summary: applied.content,
    fromSeq,
    toSeq,
    messageCount: messages.length,
    localDate,
    published: true,
    indexed: applied.indexed,
    indexSkipped: applied.indexSkipped,
    indexError: applied.indexError,
    sourceMessages: messages.map((m) => ({ role: m.role, content: m.content, seq: m.seq })),
  };
}
