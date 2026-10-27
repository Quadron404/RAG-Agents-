/**
 * Daily-summary publish pipeline: save to D1, then index to AI Search.
 *
 * Saving to D1 does NOT mean the summary is searchable.  Indexing is tracked
 * separately via `index_status` (`pending` | `indexed` | `failed` |
 * `unconfigured`).  If indexing fails, the summary is retained and a retry is
 * scheduled via `retry_count`.  A failed generation never destroys an earlier
 * successful summary, and unchanged summaries are not regenerated.
 */
import type { Env } from "../env";
import { localDay } from "../timezone";
import { conversationDocKey, indexSummaryRow, rebuildChannelDocument, type IndexableSummary } from "./search";
import {
  advanceCheckpoint,
  getChannelDailySummary,
  getConversationById,
  incrementChannelDailySummaryRetry,
  pendingChannelDailySummaryIndexes,
  saveChannelDailySummary,
  setChannelDailySummaryIndexStatus,
  type ChannelDailySummaryRow,
} from "./store";

export interface PublishResult {
  saved: boolean;
  skipped: boolean;
  version: number;
  indexed: boolean;
  indexSkipped: boolean;
  indexError: string | null;
}

/**
 * Save a generated daily summary to D1 and attempt AI Search indexing.
 *
 * Never destroys an earlier successful summary: if the new coverage does not
 * extend the existing one, the existing row is left untouched.  Indexing is
 * attempted after save; a failure records `index_status = 'failed'` and
 * increments `retry_count` but leaves the summary in place.
 */
export async function publishDailySummary(
  env: Env,
  ownerSub: string,
  conversationId: string,
  localDate: string,
  periodStart: string,
  periodEnd: string,
  content: string,
  coveredFromSeq: number,
  coveredToSeq: number,
  sourceSummaryIds: string[],
  now: number,
): Promise<PublishResult> {
  const sourceRevision = await computeSourceRevision(env, conversationId, coveredFromSeq, coveredToSeq);
  const id = `daily:${ownerSub}:${conversationId}:${localDate}`;

  const saved = await saveChannelDailySummary(env, {
    id,
    ownerSub,
    conversationId,
    localDate,
    periodStart,
    periodEnd,
    content,
    coveredFromSeq,
    coveredToSeq,
    sourceSummaryIds,
    sourceRevision,
    now,
  });

  if (saved.skipped) {
    // Unchanged summary: nothing to do, but ensure indexing state is known.
    const existing = await getChannelDailySummary(env, ownerSub, conversationId, localDate);
    return {
      saved: false,
      skipped: true,
      version: saved.version,
      indexed: existing?.index_status === "indexed",
      indexSkipped: existing?.index_status === "unconfigured",
      indexError: existing?.index_error ?? null,
    };
  }

  if (!saved.applied) {
    // Coverage did not extend; earlier summary is intact.
    const existing = await getChannelDailySummary(env, ownerSub, conversationId, localDate);
    return {
      saved: false,
      skipped: false,
      version: existing?.version ?? 0,
      indexed: existing?.index_status === "indexed",
      indexSkipped: existing?.index_status === "unconfigured",
      indexError: existing?.index_error ?? null,
    };
  }

  // Index the newly saved summary.
  const fresh = await getChannelDailySummary(env, ownerSub, conversationId, localDate);
  const conversation = await getConversationById(env, conversationId);
  let indexed = false;
  let indexSkipped = false;
  let indexError: string | null = null;

  if (fresh) {
    const row: IndexableSummary = {
      conversation_id: fresh.conversation_id,
      owner_sub: fresh.owner_sub,
      content: fresh.content,
      version: fresh.version,
      doc_key: await conversationDocKey(fresh.owner_sub, fresh.conversation_id),
      updated_at: fresh.updated_at,
    };
    const result = await indexSummaryRow(env, row, conversation?.title ?? null);
    if (result.ok) {
      await setChannelDailySummaryIndexStatus(env, conversationId, localDate, "indexed");
      indexed = true;
    } else if (result.skipped) {
      await setChannelDailySummaryIndexStatus(env, conversationId, localDate, "unconfigured");
      indexSkipped = true;
    } else {
      await setChannelDailySummaryIndexStatus(env, conversationId, localDate, "failed", result.error ?? "");
      await incrementChannelDailySummaryRetry(env, conversationId, localDate);
      indexError = result.error ?? null;
    }
  }

  return {
    saved: true,
    skipped: false,
    version: saved.version,
    indexed,
    indexSkipped,
    indexError,
  };
}

/**
 * Persist a generated daily summary and advance the channel checkpoint.
 *
 * The summary text opens with its date prefix.  If a successful daily summary
 * already exists for the same local date, the new summary is accumulated into
 * it so previously covered messages are never resubmitted to the model -- only
 * the newly covered range is extended.  The checkpoint advances only after the
 * summary is saved, and the canonical RAG document is rebuilt so the new
 * summary becomes searchable.
 */
export async function applyDailySummary(
  env: Env,
  ownerSub: string,
  conversationId: string,
  localDate: string,
  rawSummary: string,
  fromSeq: number,
  toSeq: number,
  now: number,
): Promise<PublishResult & { content: string }> {
  const existing = await getChannelDailySummary(env, ownerSub, conversationId, localDate);
  const continued = Boolean(existing && existing.generation_status === "success");
  const content = continued
    ? `${existing!.content}\n\n${rawSummary}`
    : `${localDate}\n\n${rawSummary}`;
  const coveredFromSeq = continued ? existing!.covered_from_seq : fromSeq;
  const sourceRevision = await computeSourceRevision(env, conversationId, coveredFromSeq, toSeq);
  const id = `daily:${ownerSub}:${conversationId}:${localDate}`;

  const saved = await saveChannelDailySummary(env, {
    id,
    ownerSub,
    conversationId,
    localDate,
    periodStart: localDate,
    periodEnd: localDate,
    content,
    coveredFromSeq,
    coveredToSeq: toSeq,
    sourceSummaryIds: [],
    sourceRevision,
    generationStatus: "success",
    now,
  });

  if (!saved.applied && !saved.skipped) {
    return {
      saved: false,
      skipped: false,
      version: saved.version,
      indexed: false,
      indexSkipped: false,
      indexError: null,
      content,
    };
  }

  // Advance the durable checkpoint only now that the summary is persisted.
  await advanceCheckpoint(env, conversationId, toSeq, now);

  // Index the newly saved summary and rebuild the canonical document.
  const fresh = await getChannelDailySummary(env, ownerSub, conversationId, localDate);
  const conversation = await getConversationById(env, conversationId);
  let indexed = false;
  let indexSkipped = false;
  let indexError: string | null = null;

  if (fresh) {
    const row: IndexableSummary = {
      conversation_id: fresh.conversation_id,
      owner_sub: fresh.owner_sub,
      content: fresh.content,
      version: fresh.version,
      doc_key: await conversationDocKey(fresh.owner_sub, fresh.conversation_id),
      updated_at: fresh.updated_at,
    };
    const result = await indexSummaryRow(env, row, conversation?.title ?? null);
    if (result.ok) {
      await setChannelDailySummaryIndexStatus(env, conversationId, localDate, "indexed");
      indexed = true;
    } else if (result.skipped) {
      await setChannelDailySummaryIndexStatus(env, conversationId, localDate, "unconfigured");
      indexSkipped = true;
    } else {
      await setChannelDailySummaryIndexStatus(env, conversationId, localDate, "failed", result.error ?? "");
      await incrementChannelDailySummaryRetry(env, conversationId, localDate);
      indexError = result.error ?? null;
    }

    const conv = await getConversationById(env, conversationId);
    await rebuildChannelDocument(env, ownerSub, conversationId, conv?.title ?? null);
  }

  return {
    saved: true,
    skipped: saved.skipped,
    version: saved.version,
    indexed,
    indexSkipped,
    indexError,
    content,
  };
}

/** Retry failed daily-summary indexes.  Returns the number retried. */
export async function retryFailedDailySummaryIndexes(
  env: Env,
  now: number,
  limit = 5,
): Promise<number> {
  const pending = await pendingChannelDailySummaryIndexes(env, now, limit);
  let retried = 0;
  for (const summary of pending) {
    const conversation = await getConversationById(env, summary.conversation_id);
    const row: IndexableSummary = {
      conversation_id: summary.conversation_id,
      owner_sub: summary.owner_sub,
      content: summary.content,
      version: summary.version,
      doc_key: await conversationDocKey(summary.owner_sub, summary.conversation_id),
      updated_at: summary.updated_at,
    };
    const result = await indexSummaryRow(env, row, conversation?.title ?? null);
    if (result.ok) {
      await setChannelDailySummaryIndexStatus(env, summary.conversation_id, summary.local_date, "indexed");
      retried += 1;
    } else if (result.skipped) {
      await setChannelDailySummaryIndexStatus(env, summary.conversation_id, summary.local_date, "unconfigured");
      retried += 1;
    } else {
      await incrementChannelDailySummaryRetry(env, summary.conversation_id, summary.local_date);
      await setChannelDailySummaryIndexStatus(
        env,
        summary.conversation_id,
        summary.local_date,
        "failed",
        result.error ?? "",
      );
    }
  }
  return retried;
}

/** Stable source revision: hash of the message sequence range. */
async function computeSourceRevision(
  env: Env,
  conversationId: string,
  fromSeq: number,
  toSeq: number,
): Promise<string> {
  const { sha256, toHex, utf8 } = await import("../encoding");
  const digest = toHex(await sha256(utf8(`${conversationId}:${fromSeq}:${toSeq}`)));
  return digest.slice(0, 16);
}
