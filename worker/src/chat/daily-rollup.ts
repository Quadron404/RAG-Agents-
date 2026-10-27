/**
 * 30-day roll-up summary generation and indexing.
 *
 * Takes a scheduled roll-up job (from Prompt 7's period selection), retrieves
 * every eligible daily summary for the period, generates a comprehensive
 * 30-day roll-up using multi-stage summarization, persists it to D1, and
 * indexes it to AI Search with safe retries.
 *
 * Input is daily summaries only -- never the original transcript.  Original
 * messages and daily summaries are retained.  An unchanged successful roll-up
 * is not regenerated.
 */
import type { Env } from "../env";
import { conversationDocKey, indexSummaryRow, type IndexableSummary } from "./search";
import {
  getChannelDailySummary,
  saveChannelDailySummary,
  setChannelDailySummaryIndexStatus,
  incrementChannelDailySummaryRetry,
  type ChannelDailySummaryRow,
} from "./store";
import { selectSummaryProvider } from "./providers";

const ROLLUP_SUMMARY_INSTRUCTION =
  "Combine these daily summaries into a single comprehensive 30-day roll-up summary as one plain-text paragraph. Preserve exact technical terms, entities, preferences, decisions and their chronology, configurations, completed work, outstanding work, recurring issues, proposals, and unresolved conflicts. Distinguish confirmed decisions from proposals. Do not assume that a newer timestamp alone proves an earlier decision was replaced. Do not invent facts or silently resolve contradictions. Keep the paragraph concise but sufficiently detailed to recover the period's important information. Return only the paragraph, with no headings, bullets, Markdown, or commentary.";

const MAX_CHUNK_CHARS = 100_000;

export interface RollupResult {
  saved: boolean;
  skipped: boolean;
  version: number;
  indexed: boolean;
  indexSkipped: boolean;
  indexError: string | null;
}

/**
 * Generate and index a 30-day roll-up summary for the given period.
 *
 * Returns null when there are insufficient daily summaries for the period.
 */
export async function generateRollupSummary(
  env: Env,
  ownerSub: string,
  conversationId: string,
  periodStart: string,
  periodEnd: string,
  inputSummaryIds: string[],
  inputSummaryVersions: number[],
  now: number,
): Promise<RollupResult | null> {
  const selection = selectSummaryProvider(env);
  if (!selection) {
    throw new Error("no summary model is configured");
  }

  // Retrieve all eligible daily summaries for the period, ordered by date.
  const dailies: ChannelDailySummaryRow[] = [];
  for (const id of inputSummaryIds) {
    // Parse owner/conversation/date from the job's input summary IDs.
    // The ID format is: daily:${ownerSub}:${conversationId}:${localDate}
    const parts = id.split(":");
    if (parts.length < 4) continue;
    const localDate = parts.slice(3).join(":");
    const daily = await getChannelDailySummary(env, ownerSub, conversationId, localDate);
    if (daily && daily.generation_status === "success") {
      dailies.push(daily);
    }
  }
  dailies.sort((a, b) => a.local_date.localeCompare(b.local_date));

  if (dailies.length === 0) return null;

  // Build the input text from daily summaries.
  const inputText = dailies
    .map((d, i) => `Day ${d.local_date} (summary v${d.version}):\n${d.content}`)
    .join("\n\n");

  // Check if an existing successful roll-up covers the same period with the
  // same inputs -- skip regeneration if unchanged.
  const existing = await getChannelDailySummary(env, ownerSub, conversationId, periodStart);
  const newRevision = await computeRollupRevision(inputSummaryIds, inputSummaryVersions);
  if (
    existing &&
    existing.summary_type === "rollup_30d" &&
    existing.generation_status === "success" &&
    existing.source_revision === newRevision &&
    existing.source_summary_ids === JSON.stringify(inputSummaryIds)
  ) {
    return {
      saved: false,
      skipped: true,
      version: existing.version,
      indexed: existing.index_status === "indexed",
      indexSkipped: existing.index_status === "unconfigured",
      indexError: existing.index_error ?? null,
    };
  }

  // Generate the roll-up using multi-stage summarization.
  const summary = await multiStageSummarize(selection!, inputText);

  // Save to D1.
  const id = `rollup:${ownerSub}:${conversationId}:${periodStart}`;
  const saved = await saveChannelDailySummary(env, {
    id,
    ownerSub,
    conversationId,
    localDate: periodStart,
    periodStart,
    periodEnd,
    content: summary,
    coveredFromSeq: dailies[0].covered_from_seq,
    coveredToSeq: dailies[dailies.length - 1].covered_to_seq,
    sourceSummaryIds: inputSummaryIds,
    sourceRevision: await computeRollupRevision(inputSummaryIds, inputSummaryVersions),
    generationStatus: "success",
    now,
  });

  if (saved.skipped) {
    const fresh = await getChannelDailySummary(env, ownerSub, conversationId, periodStart);
    return {
      saved: false,
      skipped: true,
      version: saved.version,
      indexed: fresh?.index_status === "indexed",
      indexSkipped: fresh?.index_status === "unconfigured",
      indexError: fresh?.index_error ?? null,
    };
  }

  if (!saved.applied) {
    const fresh = await getChannelDailySummary(env, ownerSub, conversationId, periodStart);
    return {
      saved: false,
      skipped: false,
      version: fresh?.version ?? 0,
      indexed: fresh?.index_status === "indexed",
      indexSkipped: fresh?.index_status === "unconfigured",
      indexError: fresh?.index_error ?? null,
    };
  }

  // Index to AI Search.
  const fresh = await getChannelDailySummary(env, ownerSub, conversationId, periodStart);
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
    const result = await indexSummaryRow(env, row, `30-day roll-up ${periodStart} to ${periodEnd}`);
    if (result.ok) {
      await setChannelDailySummaryIndexStatus(env, conversationId, periodStart, "indexed");
      indexed = true;
    } else if (result.skipped) {
      await setChannelDailySummaryIndexStatus(env, conversationId, periodStart, "unconfigured");
      indexSkipped = true;
    } else {
      await setChannelDailySummaryIndexStatus(env, conversationId, periodStart, "failed", result.error ?? "");
      await incrementChannelDailySummaryRetry(env, conversationId, periodStart);
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

/** Retry failed roll-up indexes.  Returns the number retried. */
export async function retryFailedRollupIndexes(
  env: Env,
  now: number,
  limit = 5,
): Promise<number> {
  // Reuse the daily summary pending-index scan but filter for rollup_30d.
  const { results } = await env.DB.prepare(
    `SELECT id, owner_sub, conversation_id, summary_type, period_start, period_end,
            local_date, content, version, covered_from_seq, covered_to_seq,
            source_summary_ids, index_status, index_error, retry_count,
            source_revision, created_at, updated_at, indexed_at
      FROM channel_daily_summaries
      WHERE summary_type = 'rollup_30d'
        AND index_status IN ('pending', 'failed')
        AND retry_count < 5
        AND updated_at <= ?1
      ORDER BY updated_at ASC
      LIMIT ?2`,
  )
    .bind(now - 30_000, limit)
    .all<ChannelDailySummaryRow>();

  let retried = 0;
  for (const summary of results) {
    const selection = selectSummaryProvider(env);
    if (!selection) continue;
    const row: IndexableSummary = {
      conversation_id: summary.conversation_id,
      owner_sub: summary.owner_sub,
      content: summary.content,
      version: summary.version,
      doc_key: await conversationDocKey(summary.owner_sub, summary.conversation_id),
      updated_at: summary.updated_at,
    };
    const result = await indexSummaryRow(env, row, `30-day roll-up ${summary.period_start}`);
    if (result.ok) {
      await setChannelDailySummaryIndexStatus(env, summary.conversation_id, summary.local_date, "indexed");
      retried += 1;
    } else if (result.skipped) {
      await setChannelDailySummaryIndexStatus(env, summary.conversation_id, summary.local_date, "unconfigured");
      retried += 1;
    } else {
      await incrementChannelDailySummaryRetry(env, summary.conversation_id, summary.local_date);
      await setChannelDailySummaryIndexStatus(env, summary.conversation_id, summary.local_date, "failed", result.error ?? "");
    }
  }
  return retried;
}

/** Multi-stage summarization with the same chunking logic as daily generation. */
async function multiStageSummarize(
  selection: NonNullable<ReturnType<typeof selectSummaryProvider>>,
  text: string,
): Promise<string> {
  if (text.length <= MAX_CHUNK_CHARS) {
    return summarizeText(selection, ROLLUP_SUMMARY_INSTRUCTION, text);
  }
  const chunks: string[] = [];
  let remaining = text;
  while (remaining.length > 0) {
    let cut = MAX_CHUNK_CHARS;
    if (cut < remaining.length) {
      const nl = remaining.lastIndexOf("\n", cut);
      if (nl > MAX_CHUNK_CHARS / 2) cut = nl + 1;
    }
    chunks.push(remaining.slice(0, cut));
    remaining = remaining.slice(cut);
  }
  const partials: string[] = [];
  for (const chunk of chunks) {
    partials.push(await summarizeText(
      selection,
      ROLLUP_SUMMARY_INSTRUCTION,
      `Partial input (part ${partials.length + 1} of ${chunks.length}):\n\n${chunk}`,
    ));
  }
  return summarizeText(
    selection,
    ROLLUP_SUMMARY_INSTRUCTION,
    partials.join("\n\n---\n\n"),
  );
}

async function summarizeText(
  selection: NonNullable<ReturnType<typeof selectSummaryProvider>>,
  instruction: string,
  text: string,
): Promise<string> {
  let out = "";
  for await (const event of selection.provider.stream(
    [
      { role: "system", content: instruction },
      { role: "user", content: text },
    ],
    [],
  )) {
    if (event.type === "text") out += event.text;
  }
  const cleaned = out.trim();
  if (!cleaned) throw new Error("the summary model returned an empty paragraph");
  return cleaned;
}

/** Claim pending roll-up jobs and process them.  Returns the number processed. */
export async function runRollupSummaryTick(
  env: Env,
  now: number,
  limit = 5,
): Promise<number> {
  const { results } = await env.DB.prepare(
    `SELECT job_id, owner_sub, conversation_id, period_start, period_end,
            input_summary_ids, input_summary_versions, attempts, available_at
      FROM summary_schedule_jobs
      WHERE summary_type = 'rollup_30d'
        AND status = 'pending'
        AND available_at <= ?1
      ORDER BY available_at ASC, created_at ASC
      LIMIT ?2`,
  )
    .bind(now, limit)
    .all<{
      job_id: string;
      owner_sub: string;
      conversation_id: string;
      period_start: string;
      period_end: string;
      input_summary_ids: string;
      input_summary_versions: string;
    }>();

  let processed = 0;
  for (const job of results) {
    const ids: string[] = JSON.parse(job.input_summary_ids ?? "[]");
    const versions: number[] = JSON.parse(job.input_summary_versions ?? "[]");
    const result = await generateRollupSummary(
      env,
      job.owner_sub,
      job.conversation_id,
      job.period_start,
      job.period_end,
      ids,
      versions,
      now,
    );
    // Mark the job completed regardless of generation outcome so it is not
    // reclaimed endlessly; generation failures are tracked in D1 row state.
    await env.DB.prepare(
      `UPDATE summary_schedule_jobs
        SET status = 'completed', updated_at = ?2
        WHERE job_id = ?1`,
    )
      .bind(job.job_id, now)
      .run();
    if (result) processed += 1;
  }
  return processed;
}

async function computeRollupRevision(
  ids: string[],
  versions: number[],
): Promise<string> {
  const { sha256, toHex, utf8 } = await import("../encoding");
  const input = ids.join(",") + ":" + versions.join(",");
  return toHex(await sha256(utf8(input))).slice(0, 16);
}
