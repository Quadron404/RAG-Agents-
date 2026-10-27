/**
 * Inactivity summaries, entirely on the Worker.
 *
 * A conversation becomes eligible one minute after its last turn when it has
 * messages newer than `summarized_through`.  The full transcript is summarised
 * every time; the checkpoint is monotonic, so a slow or retried job can never
 * move coverage backwards.  The resulting paragraph is written to D1 first and
 * then uploaded to AI Search, so an indexing outage never costs the summary.
 */
import type { Env } from "../env";
import { selectSummaryProvider } from "./providers";
import { conversationDocKey, indexSummary } from "./search";
import {
  claimSummaryJob,
  enqueueSummaryJob,
  getConversationById,
  getConversationSummary,
  lastVisibleSeq,
  listDueConversations,
  listMessages,
  markSummaryJob,
  pendingSummaryIndexes,
  saveConversationSummary,
  setConversationSummaryStatus,
  setSummaryIndexResult,
  type MessageRow,
  type SummaryJobRow,
} from "./store";

export const SUMMARY_INSTRUCTION =
  "Summarize this entire conversation as one plain-text paragraph for future AI retrieval. Preserve important facts, exact technical terms, keywords, names, dates, decisions, constraints, outcomes, and unresolved issues. Distinguish confirmed decisions from proposals. Do not invent facts or silently resolve contradictions. Keep the paragraph concise but sufficiently detailed to recover the conversation's important information. Return only the paragraph, with no headings, bullets, Markdown, or commentary.";

const MAX_TRANSCRIPT_CHARS = 120_000;
const MAX_MESSAGE_CHARS = 24_000;
const MAX_ATTEMPTS = 3;

export interface SummaryTickResult {
  eligible: number;
  summarized: number;
  indexed: number;
  failed: number;
}

function truncate(value: string, limit: number): string {
  const text = value.trim();
  if (text.length <= limit) return text;
  const half = Math.floor(limit / 2) - 20;
  return `${text.slice(0, half)}\n[...truncated...]\n${text.slice(-half)}`;
}

export function transcriptFrom(messages: MessageRow[]): string {
  const lines: string[] = [];
  for (const message of messages) {
    if (message.role === "system") continue;
    if (message.hidden && message.role !== "tool") continue;
    const content = truncate(message.content, MAX_MESSAGE_CHARS);
    if (!content) continue;
    if (message.role === "tool") {
      const name = (() => {
        try {
          const data = JSON.parse(message.data || "{}") as Record<string, unknown>;
          return typeof data.name === "string" ? data.name : "tool";
        } catch {
          return "tool";
        }
      })();
      lines.push(`[${name} result]\n${content}`);
    } else {
      lines.push(`${message.role === "user" ? "User" : "Chief"}:\n${content}`);
    }
  }
  return truncate(lines.join("\n\n"), MAX_TRANSCRIPT_CHARS);
}

async function summarizeWithProvider(env: Env, transcript: string): Promise<string> {
  const selection = selectSummaryProvider(env);
  if (!selection) {
    throw new Error("no summary model is configured");
  }
  let text = "";
  for await (const event of selection.provider.stream(
    [
      { role: "system", content: SUMMARY_INSTRUCTION },
      {
        role: "user",
        content: `Conversation transcript follows. Summarize the entire conversation.\n\n${transcript}`,
      },
    ],
    [],
  )) {
    if (event.type === "text") text += event.text;
  }
  const cleaned = text.trim();
  if (!cleaned) throw new Error("the summary model returned an empty paragraph");
  return cleaned;
}

async function isDeveloperMode(env: Env, ownerSub: string): Promise<boolean> {
  const row = await env.DB.prepare("SELECT developer_mode FROM users WHERE google_sub = ?1")
    .bind(ownerSub)
    .first<{ developer_mode: number }>();
  return (row?.developer_mode ?? 0) === 1;
}

export async function runSummaryTick(
  env: Env,
  now: number,
  options: { idleSeconds?: number; jobs?: number; indexes?: number } = {},
): Promise<SummaryTickResult> {
  const idleSeconds = options.idleSeconds ?? Number(env.CHAT_SUMMARY_IDLE_SECONDS ?? 60);
  const idleMs = (Number.isFinite(idleSeconds) && idleSeconds > 0 ? idleSeconds : 60) * 1000;
  const result: SummaryTickResult = { eligible: 0, summarized: 0, indexed: 0, failed: 0 };

  const due = await listDueConversations(env, now, idleMs, 10);
  for (const conversation of due) {
    // Developer Mode suppresses automatic transcript summarisation entirely;
    // only the explicit Generate History button (or the daily scheduler in
    // normal mode) may summarise a developer-mode channel.
    if (await isDeveloperMode(env, conversation.owner_sub)) continue;
    const coveredTo = await lastVisibleSeq(env, conversation.id);
    if (coveredTo <= conversation.summarized_through) continue;
    await enqueueSummaryJob(env, conversation, coveredTo, now);
    result.eligible += 1;
  }

  const jobs = Math.max(1, options.jobs ?? 2);
  for (let index = 0; index < jobs; index += 1) {
    const job = await claimSummaryJob(env, now, MAX_ATTEMPTS);
    if (!job) break;
    const outcome = await processJob(env, job, now);
    if (outcome.summarized) result.summarized += 1;
    if (outcome.indexed) result.indexed += 1;
    if (outcome.failed) result.failed += 1;
  }

  const pending = await pendingSummaryIndexes(env, now, Math.max(1, options.indexes ?? 4));
  for (const summary of pending) {
    const conversation = await getConversationById(env, summary.conversation_id);
    const indexed = await indexSummary(env, summary, conversation);
    if (indexed.ok) {
      await setSummaryIndexResult(env, summary.conversation_id, "indexed");
      result.indexed += 1;
    } else if (indexed.skipped) {
      await setSummaryIndexResult(env, summary.conversation_id, "unconfigured", indexed.error ?? "");
    } else {
      await setSummaryIndexResult(env, summary.conversation_id, "failed", indexed.error ?? "");
    }
  }

  return result;
}

interface JobOutcome {
  summarized: boolean;
  indexed: boolean;
  failed: boolean;
}

const SKIPPED: JobOutcome = { summarized: false, indexed: false, failed: false };

async function processJob(
  env: Env,
  job: SummaryJobRow,
  now: number,
): Promise<JobOutcome> {
  const conversation = await getConversationById(env, job.conversation_id);
  if (!conversation || conversation.deleted_at) {
    await markSummaryJob(env, job.job_id, "completed", { now });
    return SKIPPED;
  }
  if (conversation.active_request_id) {
    await markSummaryJob(env, job.job_id, "pending", {
      now,
      availableAt: now + 30_000,
      error: "conversation_active",
    });
    return SKIPPED;
  }
  const coveredTo = await lastVisibleSeq(env, conversation.id);
  if (coveredTo <= conversation.summarized_through) {
    await markSummaryJob(env, job.job_id, "completed", { now });
    return SKIPPED;
  }

  await setConversationSummaryStatus(env, conversation.id, "running", now);
  try {
    const messages = await listMessages(env, conversation.owner_sub, conversation.id);
    const transcript = transcriptFrom(messages);
    if (!transcript.trim()) {
      await markSummaryJob(env, job.job_id, "completed", { now });
      await setConversationSummaryStatus(env, conversation.id, "idle", now);
      return SKIPPED;
    }
    const content = await summarizeWithProvider(env, transcript);
    const previous = await getConversationSummary(env, conversation.id);
    const docKey =
      previous?.doc_key || (await conversationDocKey(conversation.owner_sub, conversation.id));
    const saved = await saveConversationSummary(env, {
      conversationId: conversation.id,
      ownerSub: conversation.owner_sub,
      content,
      coveredFrom: previous?.covered_to ?? 0,
      coveredTo,
      docKey,
      now,
    });
    await markSummaryJob(env, job.job_id, "completed", { now });
    await setConversationSummaryStatus(env, conversation.id, "idle", now);

    let indexed = false;
    if (saved.applied) {
      const fresh = await getConversationSummary(env, conversation.id);
      if (fresh) {
        const result = await indexSummary(env, fresh, conversation);
        indexed = result.ok;
        if (result.ok) {
          await setSummaryIndexResult(env, conversation.id, "indexed");
        } else if (result.skipped) {
          await setSummaryIndexResult(env, conversation.id, "unconfigured", result.error ?? "");
        } else {
          await setSummaryIndexResult(env, conversation.id, "failed", result.error ?? "");
        }
      }
    }
    return { summarized: true, indexed, failed: false };
  } catch (error) {
    const message = String((error as Error).message ?? error).slice(0, 500);
    const attempts = job.attempts;
    if (attempts < MAX_ATTEMPTS) {
      const backoff = Math.min(60_000 * 2 ** Math.max(0, attempts - 1), 15 * 60_000);
      await markSummaryJob(env, job.job_id, "pending", {
        now,
        availableAt: now + backoff,
        error: message,
      });
      await setConversationSummaryStatus(env, conversation.id, "pending", now);
      return { summarized: false, indexed: false, failed: true };
    }
    await markSummaryJob(env, job.job_id, "failed", { now, error: message });
    await setConversationSummaryStatus(env, conversation.id, "failed", now);
    return { summarized: false, indexed: false, failed: true };
  }
}
