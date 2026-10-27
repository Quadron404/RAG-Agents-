/**
 * Conversation summaries in AI Search.
 *
 * Each conversation has one stable document key, so indexing a later summary
 * replaces the previous entry instead of appending a second one.  Every upload
 * carries the owner subject as metadata and every query filters on it.  As a
 * defence in depth the returned chunks are checked against the authenticated
 * subject again; a chunk belonging to another tenant is never passed to the
 * model.
 */
import type { AiSearchChunk, AiSearchInstance, Env } from "../env";
import { sha256, toHex, utf8 } from "../encoding";
import {
  getConversationById,
  listActiveChannelSummaries,
  setChannelSummaryIndexStatusById,
  type ChannelDailySummaryRow,
  type ConversationRow,
  type SummaryRow,
} from "./store";
import { selectSummaryProvider } from "./providers";

const CUSTOM_METADATA = [
  { field_name: "owner_sub", data_type: "text" },
  { field_name: "conversation_id", data_type: "text" },
  { field_name: "conversation_title", data_type: "text" },
  { field_name: "updated_at", data_type: "text" },
];

function configured(env: Env): boolean {
  return !!(env.AI_SEARCH && (env.AI_SEARCH_INSTANCE ?? "").trim());
}

export function searchConfigured(env: Env): boolean {
  return configured(env);
}

async function instanceFor(env: Env): Promise<AiSearchInstance | null> {
  if (!configured(env)) return null;
  const name = (env.AI_SEARCH_INSTANCE ?? "").trim();
  const binding = env.AI_SEARCH!;
  let handle = binding.get(name);
  let exists = true;
  try {
    await handle.info();
  } catch {
    exists = false;
  }
  if (!exists) {
    try {
      handle = await binding.create({
        id: name,
        custom_metadata: CUSTOM_METADATA,
      });
    } catch {
      // Another isolate may have created it between info() and create().
      handle = binding.get(name);
    }
  }
  try {
    return handle;
  } catch {
    return null;
  }
}

export async function conversationDocKey(ownerSub: string, conversationId: string): Promise<string> {
  const digest = toHex(await sha256(utf8(`${ownerSub}:${conversationId}`)));
  return `chief-${digest.slice(0, 48)}.txt`;
}

export interface IndexableSummary {
  conversation_id: string;
  owner_sub: string;
  content: string;
  version: number;
  doc_key: string;
  updated_at: number;
}

export interface IndexResult {
  ok: boolean;
  skipped?: boolean;
  error?: string;
}

function summaryDocumentFor(summary: IndexableSummary, conversationTitle: string | null): string {
  const title = conversationTitle ?? `Conversation ${summary.conversation_id}`;
  return [
    "Chief conversation summary",
    `Conversation: ${title}`,
    `Conversation ID: ${summary.conversation_id}`,
    `Summary version: ${summary.version}`,
    `Updated: ${new Date(summary.updated_at).toISOString()}`,
    "",
    summary.content.trim(),
    "",
  ].join("\n");
}

export async function indexSummaryRow(
  env: Env,
  summary: IndexableSummary,
  conversationTitle: string | null,
): Promise<IndexResult> {
  if (!configured(env)) {
    return { ok: false, skipped: true, error: "AI Search is not configured" };
  }
  const instance = await instanceFor(env);
  if (!instance) return { ok: false, error: "AI Search instance is unavailable" };
  try {
    await instance.items.upload(summary.doc_key, summaryDocumentFor(summary, conversationTitle), {
      metadata: {
        owner_sub: summary.owner_sub,
        conversation_id: summary.conversation_id,
        conversation_title: (conversationTitle ?? "").slice(0, 200),
        updated_at: new Date(summary.updated_at).toISOString(),
      },
    });
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String((error as Error).message ?? error) };
  }
}

export async function indexSummary(
  env: Env,
  summary: SummaryRow,
  conversation: ConversationRow | null,
): Promise<IndexResult> {
  const key = summary.doc_key || (await conversationDocKey(summary.owner_sub, summary.conversation_id));
  const row: IndexableSummary = {
    ...summary,
    doc_key: key,
  };
  return indexSummaryRow(env, row, conversation?.title ?? null);
}

export async function removeSummaryDoc(
  env: Env,
  ownerSub: string,
  conversationId: string,
): Promise<{ ok: boolean; error: string | null }> {
  if (!configured(env)) return { ok: true, error: null };
  const instance = await instanceFor(env);
  if (!instance?.items.delete) return { ok: true, error: null };
  try {
    await instance.items.delete(await conversationDocKey(ownerSub, conversationId));
    return { ok: true, error: null };
  } catch (error) {
    return {
      ok: false,
      error: String((error as Error)?.message ?? error),
    };
  }
}

/** Build the single canonical searchable document for a channel from its active
 * D1 summaries, in chronological order.  Each summary is self-labelled with its
 * date/period prefix, so the document text never mixes channels or periods. */
export function buildChannelDocumentText(
  rows: ChannelDailySummaryRow[],
  conversationTitle: string | null,
): string {
  const title = conversationTitle ?? "Conversation";
  const sections = rows.map((row) => {
    const label = row.summary_type === "yearly" ? `${row.period_start} (yearly)`
      : row.summary_type === "monthly" ? `${row.period_start} (monthly)`
      : row.period_start;
    return `${label}\n${(row.content || "").trim()}`;
  });
  return [
    `Channel history — ${title}`,
    `Active summaries: ${rows.length}`,
    "",
    sections.join("\n\n"),
    "",
  ].join("\n");
}

export async function rebuildChannelDocument(
  env: Env,
  ownerSub: string,
  conversationId: string,
  conversationTitle: string | null,
  excludeIds: string[] = [],
): Promise<IndexResult> {
  if (!configured(env)) {
    return { ok: false, skipped: true, error: "AI Search is not configured" };
  }
  const instance = await instanceFor(env);
  if (!instance) return { ok: false, error: "AI Search instance is unavailable" };
  const docKey = await conversationDocKey(ownerSub, conversationId);
  const rows = (await listActiveChannelSummaries(env, ownerSub, conversationId)).filter(
    (row) => !excludeIds.includes(row.id),
  );
  const text = buildChannelDocumentText(rows, conversationTitle);
  const conversation = await getConversationById(env, conversationId);
  try {
    await instance.items.upload(docKey, text, {
      metadata: {
        owner_sub: ownerSub,
        conversation_id: conversationId,
        conversation_title: (conversationTitle ?? "").slice(0, 200),
        updated_at: new Date(Date.now()).toISOString(),
        summary_count: String(rows.length),
      },
    });
    for (const row of rows) {
      await setChannelSummaryIndexStatusById(env, row.id, "indexed");
    }
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String((error as Error).message ?? error) };
  }
}

export interface HistorySearchResult {
  ok: boolean;
  text: string;
  resultCount: number;
  error?: string;
}

function chunkOwner(chunk: AiSearchChunk): string {
  const metadata = chunk.item?.metadata;
  if (!metadata || typeof metadata !== "object") return "";
  const value = (metadata as Record<string, unknown>).owner_sub;
  return typeof value === "string" ? value : "";
}

function chunkTitle(chunk: AiSearchChunk): string {
  const metadata = chunk.item?.metadata;
  if (!metadata || typeof metadata !== "object") return "";
  const value = (metadata as Record<string, unknown>).conversation_title;
  return typeof value === "string" ? value : "";
}

/** Extract only the summary content from an indexed document, stripping
 *  internal metadata (conversation ID, version, timestamps, title). */
function extractSummaryContent(text: string): string {
  // The indexed document format has a header block followed by a blank line
  // then the summary content.  Strip everything up to and including the
  // first blank line.
  const lines = text.split("\n");
  let contentStart = -1;
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].trim() === "") {
      contentStart = i + 1;
      break;
    }
  }
  if (contentStart < 0 || contentStart >= lines.length) return text.trim();
  return lines.slice(contentStart).join("\n").trim();
}

export async function searchConversationHistory(
  env: Env,
  ownerSub: string,
  conversationId: string,
  query: string,
): Promise<HistorySearchResult> {
  const trimmed = query.trim().slice(0, 500);
  if (!trimmed) {
    return { ok: false, error: "The search query was empty.", text: "", resultCount: 0 };
  }
  if (!configured(env)) {
    return {
      ok: false,
      error: "Historical conversation search is not configured on this Worker.",
      text: "",
      resultCount: 0,
    };
  }
  const instance = await instanceFor(env);
  if (!instance) {
    return {
      ok: false,
      error: "Historical conversation search is unavailable.",
      text: "",
      resultCount: 0,
    };
  }

  const request = {
    messages: [{ role: "user", content: trimmed }],
    ai_search_options: {
      retrieval: {
        retrieval_type: "hybrid",
        max_num_results: 3,
        match_threshold: 0.35,
        keyword_match_mode: "or",
        filters: { owner_sub: { $eq: ownerSub }, conversation_id: { $eq: conversationId } },
      },
      reranking: { enabled: false },
      query_rewrite: { enabled: false },
    },
  };

  let chunks: AiSearchChunk[] = [];
  try {
    const results = await instance.search(request);
    chunks = results.chunks ?? [];
  } catch {
    // If the metadata field is not available yet, search without the remote
    // filter and enforce tenancy here.  Other tenants' text is discarded before
    // it can reach the prompt.
    try {
      const results = await instance.search({
        ...request,
        ai_search_options: {
          ...request.ai_search_options,
          retrieval: {
            ...request.ai_search_options.retrieval,
            filters: undefined,
          },
        },
      });
      chunks = results.chunks ?? [];
    } catch (error) {
      return {
        ok: false,
        error: `Historical conversation search failed: ${String((error as Error).message ?? error)}`,
        text: "",
        resultCount: 0,
      };
    }
  }

  const seen = new Set<string>();
  const active = new Map<string, boolean>();
  const lines: string[] = [];
  let length = 0;
    for (const chunk of chunks) {
      if (chunkOwner(chunk) !== ownerSub) continue;
      const metadata = (chunk.item?.metadata ?? {}) as Record<string, unknown>;
      const chunkConversationId =
        typeof metadata.conversation_id === "string" ? metadata.conversation_id : "";
      // Mandatory channel isolation: only chunks from the exact active channel.
      if (chunkConversationId !== conversationId) continue;
      const key = chunk.item?.key ?? chunk.id ?? "";
      if (key && seen.has(key)) continue;
      if (key) seen.add(key);
      const text = (chunk.text ?? "").trim();
      if (!text) continue;
      if (!active.has(chunkConversationId)) {
        const conversation = await getConversationById(env, chunkConversationId);
        active.set(
          chunkConversationId,
          !!conversation && !conversation.deleted_at && conversation.owner_sub === ownerSub,
        );
      }
      if (!active.get(chunkConversationId)) continue;
      // Strip internal metadata from the document text — only return the summary content.
      const summaryContent = extractSummaryContent(text);
      if (!summaryContent) continue;
      // Truncate each summary to keep results concise and prevent context overflow.
      const truncated = summaryContent.length > 400
        ? summaryContent.slice(0, 400) + "..."
        : summaryContent;
      const entry = truncated;
      if (length + entry.length > 2000) break;
      lines.push(entry);
      length += entry.length;
      if (lines.length >= 3) break;
    }

  if (!lines.length) {
    return {
      ok: true,
      text: "No matching conversation summaries were found.",
      resultCount: 0,
    };
  }

  // STEP 3 — Separate Groq AI summarizes the retrieved RAG results into
  // coherent historical context.  This inference call is independent of
  // Chief's inference.  If it fails, return an honest error (never fall back
  // to the raw search dump).
  const selection = selectSummaryProvider(env);
  if (!selection) {
    return {
      ok: false,
      error: "Historical conversation summarization is not configured.",
      text: "",
      resultCount: lines.length,
    };
  }

  const rawEvidence = lines.join("\n\n");
  const HISTORY_SUMMARIZER_INSTRUCTION =
    "You are a history summarizer. Transform the following retrieved conversation " +
    "evidence into a coherent, useful historical account for an AI assistant. " +
    "Preserve exact technical keywords, names, identifiers, decisions, dates, and " +
    "configuration values. Combine related evidence into a readable narrative. " +
    "Preserve relevant chronology and distinguish confirmed decisions from " +
    "proposals. Remove irrelevant search noise and internal metadata. Never " +
    "invent facts or conceal uncertainty. Return only the synthesized historical " +
    "context, with no headings, bullets, Markdown, or commentary.";

  let synthesized = "";
  try {
    for await (const event of selection.provider.stream(
      [
        { role: "system", content: HISTORY_SUMMARIZER_INSTRUCTION },
        { role: "user", content: `Retrieved evidence:\n\n${rawEvidence}` },
      ],
      [],
    )) {
      if (event.type === "text") synthesized += event.text;
    }
  } catch (error) {
    return {
      ok: false,
      error: `Historical conversation summarization failed: ${String((error as Error).message ?? error)}`,
      text: "",
      resultCount: lines.length,
    };
  }

  const cleaned = synthesized.trim();
  if (!cleaned) {
    return {
      ok: false,
      error: "The history summarizer returned an empty result.",
      text: "",
      resultCount: lines.length,
    };
  }

  return {
    ok: true,
    text: cleaned,
    resultCount: lines.length,
  };
}
