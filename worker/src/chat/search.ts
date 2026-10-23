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
import { getConversationById, type ConversationRow, type SummaryRow } from "./store";

const CUSTOM_METADATA = [
  { field_name: "owner_sub", data_type: "text" },
  { field_name: "conversation_id", data_type: "text" },
  { field_name: "conversation_title", data_type: "text" },
  { field_name: "updated_at", data_type: "text" },
];

let ensured: { key: string; promise: Promise<AiSearchInstance | null> } | null = null;

function configured(env: Env): boolean {
  return !!(env.AI_SEARCH && (env.AI_SEARCH_INSTANCE ?? "").trim());
}

export function searchConfigured(env: Env): boolean {
  return configured(env);
}

async function instanceFor(env: Env): Promise<AiSearchInstance | null> {
  if (!configured(env)) return null;
  const name = (env.AI_SEARCH_INSTANCE ?? "").trim();
  if (ensured?.key === name) return ensured.promise;
  const promise = (async () => {
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
    return handle;
  })();
  ensured = { key: name, promise };
  try {
    return await promise;
  } catch {
    return null;
  }
}

export async function conversationDocKey(ownerSub: string, conversationId: string): Promise<string> {
  const digest = toHex(await sha256(utf8(`${ownerSub}:${conversationId}`)));
  return `chief-${digest.slice(0, 48)}.txt`;
}

function summaryDocument(summary: SummaryRow, conversation: ConversationRow | null): string {
  const title = conversation?.title ?? `Conversation ${summary.conversation_id}`;
  return [
    "Chief conversation summary",
    `Conversation: ${title}`,
    `Conversation ID: ${summary.conversation_id}`,
    `Summary version: ${summary.version}`,
    `Created: ${new Date(summary.created_at).toISOString()}`,
    `Updated: ${new Date(summary.updated_at).toISOString()}`,
    "",
    summary.content.trim(),
    "",
  ].join("\n");
}

export interface IndexResult {
  ok: boolean;
  skipped?: boolean;
  error?: string;
}

export async function indexSummary(
  env: Env,
  summary: SummaryRow,
  conversation: ConversationRow | null,
): Promise<IndexResult> {
  if (!configured(env)) {
    return { ok: false, skipped: true, error: "AI Search is not configured" };
  }
  const instance = await instanceFor(env);
  if (!instance) return { ok: false, error: "AI Search instance is unavailable" };
  const key = summary.doc_key || (await conversationDocKey(summary.owner_sub, summary.conversation_id));
  try {
    await instance.items.upload(key, summaryDocument(summary, conversation), {
      metadata: {
        owner_sub: summary.owner_sub,
        conversation_id: summary.conversation_id,
        conversation_title: (conversation?.title ?? "").slice(0, 200),
        updated_at: new Date(summary.updated_at).toISOString(),
      },
    });
    return { ok: true };
  } catch (error) {
    return { ok: false, error: String((error as Error).message ?? error) };
  }
}

export async function removeSummaryDoc(
  env: Env,
  ownerSub: string,
  conversationId: string,
): Promise<void> {
  if (!configured(env)) return;
  const instance = await instanceFor(env);
  if (!instance?.items.delete) return;
  try {
    await instance.items.delete(await conversationDocKey(ownerSub, conversationId));
  } catch {
    /* deletion is best-effort; the deleted row is filtered out of searches */
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

export async function searchConversationHistory(
  env: Env,
  ownerSub: string,
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
        max_num_results: 8,
        match_threshold: 0.25,
        keyword_match_mode: "or",
        filters: { owner_sub: { $eq: ownerSub } },
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
    const key = chunk.item?.key ?? chunk.id ?? "";
    if (key && seen.has(key)) continue;
    if (key) seen.add(key);
    const text = (chunk.text ?? "").trim();
    if (!text) continue;
    const metadata = (chunk.item?.metadata ?? {}) as Record<string, unknown>;
    const conversationId =
      typeof metadata.conversation_id === "string" ? metadata.conversation_id : "";
    if (!conversationId) continue;
    if (!active.has(conversationId)) {
      const conversation = await getConversationById(env, conversationId);
      active.set(
        conversationId,
        !!conversation && !conversation.deleted_at && conversation.owner_sub === ownerSub,
      );
    }
    if (!active.get(conversationId)) continue;
    const title = chunkTitle(chunk) || "Conversation";
    const entry = `- ${title}\n${text}`;
    if (length + entry.length > 6000) break;
    lines.push(entry);
    length += entry.length;
    if (lines.length >= 5) break;
  }

  if (!lines.length) {
    return {
      ok: true,
      text: "No matching conversation summaries were found.",
      resultCount: 0,
    };
  }
  return {
    ok: true,
    text: lines.join("\n\n"),
    resultCount: lines.length,
  };
}
