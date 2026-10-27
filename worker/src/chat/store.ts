/** D1 accessors for Worker-owned conversations, messages, and requests. */
import type { Env } from "../env";
import type { ToolCall } from "./types";

export interface ConversationRow {
  id: string;
  owner_sub: string;
  owner_id: string;
  title: string;
  created_at: number;
  updated_at: number;
  last_activity_at: number;
  summarized_through: number;
  summary_status: string;
  summary_due_at: number | null;
  active_request_id: string;
  active_since: number | null;
  deleted_at: number | null;
}

export interface MessageRow {
  seq: number;
  id: string;
  conversation_id: string;
  owner_sub: string;
  role: string;
  agent: string;
  content: string;
  data: string;
  hidden: number;
  request_id: string;
  created_at: number;
}

export interface RequestRow {
  request_id: string;
  conversation_id: string;
  owner_sub: string;
  user_message_id: string;
  assistant_message_id: string;
  status: string;
  error: string;
  created_at: number;
  updated_at: number;
}

export interface SummaryRow {
  conversation_id: string;
  owner_sub: string;
  content: string;
  version: number;
  covered_from: number;
  covered_to: number;
  doc_key: string;
  index_status: string;
  index_error: string;
  created_at: number;
  updated_at: number;
  indexed_at: number | null;
}

export interface ChannelDailySummaryRow {
  id: string;
  owner_sub: string;
  conversation_id: string;
  summary_type: string;
  period_start: string;
  period_end: string;
  local_date: string;
  content: string;
  version: number;
  covered_from_seq: number;
  covered_to_seq: number;
  source_summary_ids: string;
  generation_status: string;
  index_status: string;
  index_error: string;
  retry_count: number;
  source_revision: string;
  created_at: number;
  updated_at: number;
  indexed_at: number | null;
}
export interface SummaryJobRow {
  job_id: string;
  conversation_id: string;
  owner_sub: string;
  covered_to: number;
  status: string;
  attempts: number;
  available_at: number;
  lease_until: number | null;
  error_code: string;
  created_at: number;
  updated_at: number;
}

const CONVERSATION_COLUMNS =
  "id, owner_sub, owner_id, title, created_at, updated_at, last_activity_at, summarized_through, summary_status, summary_due_at, active_request_id, active_since, deleted_at";

const MESSAGE_COLUMNS =
  "seq, id, conversation_id, owner_sub, role, agent, content, data, hidden, request_id, created_at";

export function newId(): string {
  return crypto.randomUUID();
}

export function nowMs(): number {
  return Date.now();
}

export async function createConversation(
  env: Env,
  ownerSub: string,
  ownerId: string,
  title = "New chat",
): Promise<ConversationRow> {
  const now = nowMs();
  const id = newId();
  const row = await env.DB.prepare(
    `INSERT INTO conversations
       (id, owner_sub, owner_id, title, created_at, updated_at, last_activity_at)
     VALUES (?1, ?2, ?3, ?4, ?5, ?5, ?5)
     RETURNING ${CONVERSATION_COLUMNS}`,
  )
    .bind(id, ownerSub, ownerId, title.trim() || "New chat", now)
    .first<ConversationRow>();
  if (!row) throw new Error("conversation insert returned no row");
  return row;
}

export async function listConversations(env: Env, ownerSub: string): Promise<ConversationRow[]> {
  const result = await env.DB.prepare(
    `SELECT ${CONVERSATION_COLUMNS} FROM conversations
     WHERE owner_sub = ?1 AND deleted_at IS NULL
     ORDER BY updated_at DESC, id ASC`,
  )
    .bind(ownerSub)
    .all<ConversationRow>();
  return result.results ?? [];
}

export async function getConversation(
  env: Env,
  ownerSub: string,
  id: string,
): Promise<ConversationRow | null> {
  return env.DB.prepare(
    `SELECT ${CONVERSATION_COLUMNS} FROM conversations
     WHERE id = ?1 AND owner_sub = ?2 AND deleted_at IS NULL`,
  )
    .bind(id, ownerSub)
    .first<ConversationRow>();
}

export async function renameConversation(
  env: Env,
  ownerSub: string,
  id: string,
  title: string,
): Promise<ConversationRow | null> {
  return env.DB.prepare(
    `UPDATE conversations SET title = ?3, updated_at = ?4
     WHERE id = ?1 AND owner_sub = ?2 AND deleted_at IS NULL
     RETURNING ${CONVERSATION_COLUMNS}`,
  )
    .bind(id, ownerSub, title.trim().slice(0, 160) || "New chat", nowMs())
    .first<ConversationRow>();
}

export async function deleteConversation(env: Env, ownerSub: string, id: string): Promise<boolean> {
  const result = await env.DB.prepare(
    `UPDATE conversations SET deleted_at = ?3, updated_at = ?3
     WHERE id = ?1 AND owner_sub = ?2 AND deleted_at IS NULL`,
  )
    .bind(id, ownerSub, nowMs())
    .run();
  return (result.meta.changes ?? 0) > 0;
}

export async function listMessages(
  env: Env,
  ownerSub: string,
  conversationId: string,
  options: { visibleOnly?: boolean } = {},
): Promise<MessageRow[]> {
  const visible = options.visibleOnly ? " AND hidden = 0 AND role IN ('user','assistant')" : "";
  const result = await env.DB.prepare(
    `SELECT ${MESSAGE_COLUMNS} FROM messages
      WHERE conversation_id = ?1 AND owner_sub = ?2${visible}
      ORDER BY seq ASC`,
  )
    .bind(conversationId, ownerSub)
    .all<MessageRow>();
  return result.results ?? [];
}

/** Messages whose ``created_at`` falls within ``[startMs, endMs)``, ordered by seq. */
export async function listMessagesInRange(
  env: Env,
  ownerSub: string,
  conversationId: string,
  startMs: number,
  endMs: number,
): Promise<MessageRow[]> {
  const result = await env.DB.prepare(
    `SELECT ${MESSAGE_COLUMNS} FROM messages
      WHERE conversation_id = ?1 AND owner_sub = ?2
        AND created_at >= ?3 AND created_at < ?4
        AND hidden = 0 AND role IN ('user','assistant')
      ORDER BY seq ASC`,
  )
    .bind(conversationId, ownerSub, startMs, endMs)
    .all<MessageRow>();
  return result.results ?? [];
}

export async function countMessages(env: Env, conversationId: string): Promise<number> {
  const row = await env.DB.prepare("SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?1")
    .bind(conversationId)
    .first<{ n: number }>();
  return row?.n ?? 0;
}

export async function countUserMessages(env: Env, conversationId: string): Promise<number> {
  const row = await env.DB.prepare(
    "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?1 AND role = 'user' AND hidden = 0",
  )
    .bind(conversationId)
    .first<{ n: number }>();
  return row?.n ?? 0;
}

export async function insertMessage(
  env: Env,
  value: {
    id?: string;
    conversationId: string;
    ownerSub: string;
    role: string;
    agent?: string;
    content?: string;
    data?: unknown;
    hidden?: boolean;
    requestId?: string;
    createdAt?: number;
  },
): Promise<MessageRow> {
  const id = value.id ?? newId();
  const row = await env.DB.prepare(
    `INSERT INTO messages
       (id, conversation_id, owner_sub, role, agent, content, data, hidden, request_id, created_at)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)
     RETURNING ${MESSAGE_COLUMNS}`,
  )
    .bind(
      id,
      value.conversationId,
      value.ownerSub,
      value.role,
      value.agent ?? (value.role === "user" ? "user" : "chief"),
      value.content ?? "",
      JSON.stringify(value.data ?? {}),
      value.hidden ? 1 : 0,
      value.requestId ?? "",
      value.createdAt ?? nowMs(),
    )
    .first<MessageRow>();
  if (!row) throw new Error("message insert returned no row");
  return row;
}

export async function updateMessage(
  env: Env,
  ownerSub: string,
  id: string,
  value: { content?: string; data?: unknown; agent?: string },
): Promise<MessageRow | null> {
  return env.DB.prepare(
    `UPDATE messages SET
       content = COALESCE(?3, content),
       data = COALESCE(?4, data),
       agent = COALESCE(?5, agent)
     WHERE id = ?1 AND owner_sub = ?2
     RETURNING ${MESSAGE_COLUMNS}`,
  )
    .bind(
      id,
      ownerSub,
      value.content ?? null,
      value.data === undefined ? null : JSON.stringify(value.data),
      value.agent ?? null,
    )
    .first<MessageRow>();
}

export async function insertHiddenToolCall(
  env: Env,
  value: {
    conversationId: string;
    ownerSub: string;
    requestId: string;
    content: string;
    toolCalls: ToolCall[];
  },
): Promise<MessageRow> {
  return insertMessage(env, {
    conversationId: value.conversationId,
    ownerSub: value.ownerSub,
    role: "assistant",
    agent: "chief",
    content: value.content,
    data: { kind: "tool_call", hidden: true, tool_calls: value.toolCalls },
    hidden: true,
    requestId: value.requestId,
  });
}

export async function insertHiddenToolResult(
  env: Env,
  value: {
    conversationId: string;
    ownerSub: string;
    requestId: string;
    call: ToolCall;
    output: string;
    error?: string;
  },
): Promise<MessageRow> {
  return insertMessage(env, {
    conversationId: value.conversationId,
    ownerSub: value.ownerSub,
    role: "tool",
    agent: "chief",
    content: value.output,
    data: {
      kind: "tool_result",
      hidden: true,
      tool_call_id: value.call.id,
      name: value.call.name,
      arguments: value.call.arguments,
      error: value.error ?? "",
    },
    hidden: true,
    requestId: value.requestId,
  });
}

/* -------------------------------------------------------------------------- */
/* Per-conversation generation lock                                            */
/* -------------------------------------------------------------------------- */

export async function acquireConversationLock(
  env: Env,
  ownerSub: string,
  conversationId: string,
  requestId: string,
  now: number,
  staleAfterMs = 10 * 60 * 1000,
): Promise<boolean> {
  const result = await env.DB.prepare(
    `UPDATE conversations
     SET active_request_id = ?3, active_since = ?4, last_activity_at = ?4, updated_at = ?4
     WHERE id = ?1 AND owner_sub = ?2 AND deleted_at IS NULL
       AND (active_request_id = '' OR active_request_id = ?3 OR active_since IS NULL OR active_since < ?5)`,
  )
    .bind(conversationId, ownerSub, requestId, now, now - staleAfterMs)
    .run();
  return (result.meta.changes ?? 0) > 0;
}

export async function releaseConversationLock(
  env: Env,
  conversationId: string,
  requestId: string,
  now: number,
): Promise<void> {
  await env.DB.prepare(
    `UPDATE conversations
     SET active_request_id = '', active_since = NULL,
         last_activity_at = ?3, updated_at = ?3
     WHERE id = ?1 AND active_request_id = ?2`,
  )
    .bind(conversationId, requestId, now)
    .run();
}

/* -------------------------------------------------------------------------- */
/* Requests                                                                    */
/* -------------------------------------------------------------------------- */

export async function getRequest(env: Env, requestId: string): Promise<RequestRow | null> {
  return env.DB.prepare(
    `SELECT request_id, conversation_id, owner_sub, user_message_id, assistant_message_id,
            status, error, created_at, updated_at
     FROM chat_requests WHERE request_id = ?1`,
  )
    .bind(requestId)
    .first<RequestRow>();
}

export async function createRequest(
  env: Env,
  value: {
    requestId: string;
    conversationId: string;
    ownerSub: string;
    userMessageId: string;
    assistantMessageId: string;
    now: number;
  },
): Promise<RequestRow | null> {
  return env.DB.prepare(
    `INSERT INTO chat_requests
       (request_id, conversation_id, owner_sub, user_message_id, assistant_message_id,
        status, error, created_at, updated_at)
     VALUES (?1, ?2, ?3, ?4, ?5, 'streaming', '', ?6, ?6)
     ON CONFLICT(request_id) DO NOTHING
     RETURNING request_id, conversation_id, owner_sub, user_message_id, assistant_message_id,
               status, error, created_at, updated_at`,
  )
    .bind(
      value.requestId,
      value.conversationId,
      value.ownerSub,
      value.userMessageId,
      value.assistantMessageId,
      value.now,
    )
    .first<RequestRow>();
}

export async function markRequest(
  env: Env,
  requestId: string,
  status: "streaming" | "completed" | "failed",
  error = "",
): Promise<void> {
  await env.DB.prepare(
    "UPDATE chat_requests SET status = ?2, error = ?3, updated_at = ?4 WHERE request_id = ?1",
  )
    .bind(requestId, status, error, nowMs())
    .run();
}

export async function resetRequestForRetry(
  env: Env,
  requestId: string,
  ownerSub: string,
  assistantMessageId: string,
  now: number,
): Promise<void> {
  await env.DB.batch([
    env.DB.prepare("DELETE FROM messages WHERE request_id = ?1 AND hidden = 1").bind(requestId),
    env.DB.prepare(
      "UPDATE messages SET content = '', data = '{}' WHERE id = ?1 AND owner_sub = ?2",
    ).bind(assistantMessageId, ownerSub),
    env.DB.prepare(
      "UPDATE chat_requests SET status = 'streaming', error = '', updated_at = ?2 WHERE request_id = ?1",
    ).bind(requestId, now),
  ]);
}

export async function getMessageById(
  env: Env,
  ownerSub: string,
  id: string,
): Promise<MessageRow | null> {
  return env.DB.prepare(
    `SELECT ${MESSAGE_COLUMNS} FROM messages WHERE id = ?1 AND owner_sub = ?2`,
  )
    .bind(id, ownerSub)
    .first<MessageRow>();
}

/* -------------------------------------------------------------------------- */
/* Titles / activity                                                           */
/* -------------------------------------------------------------------------- */

export function autoTitle(text: string): string {
  const cleaned = text.replace(/\s+/g, " ").trim();
  if (!cleaned) return "New chat";
  const words = cleaned.split(" ").slice(0, 8).join(" ");
  return (words.length > 64 ? `${words.slice(0, 61)}...` : words) || "New chat";
}

export async function setTitleIfDefault(
  env: Env,
  ownerSub: string,
  conversationId: string,
  title: string,
  now: number,
): Promise<ConversationRow | null> {
  return env.DB.prepare(
    `UPDATE conversations SET title = ?3, updated_at = ?4
     WHERE id = ?1 AND owner_sub = ?2 AND deleted_at IS NULL
       AND (title = 'New chat' OR title = 'New conversation' OR title = '')
     RETURNING ${CONVERSATION_COLUMNS}`,
  )
    .bind(conversationId, ownerSub, title, now)
    .first<ConversationRow>();
}

export async function touchConversation(
  env: Env,
  ownerSub: string,
  conversationId: string,
  now: number,
): Promise<void> {
  await env.DB.prepare(
    `UPDATE conversations
     SET updated_at = ?3, last_activity_at = ?3,
         summary_status = CASE WHEN summary_status = 'failed' THEN 'idle' ELSE summary_status END
     WHERE id = ?1 AND owner_sub = ?2`,
  )
    .bind(conversationId, ownerSub, now)
    .run();
}

/* -------------------------------------------------------------------------- */
/* Summaries                                                                   */
/* -------------------------------------------------------------------------- */

export async function listDueConversations(
  env: Env,
  now: number,
  idleMs: number,
  limit = 10,
): Promise<ConversationRow[]> {
  const result = await env.DB.prepare(
    `SELECT ${CONVERSATION_COLUMNS} FROM conversations
     WHERE deleted_at IS NULL
       AND active_request_id = ''
       AND last_activity_at <= ?1
       AND summarized_through < (
         SELECT COALESCE(MAX(seq), 0) FROM messages
         WHERE conversation_id = conversations.id AND hidden = 0
       )
       AND (summary_status = 'idle' OR summary_status = '')
     ORDER BY last_activity_at ASC
     LIMIT ?2`,
  )
    .bind(now - idleMs, limit)
    .all<ConversationRow>();
  return result.results ?? [];
}

export async function lastVisibleSeq(env: Env, conversationId: string): Promise<number> {
  const row = await env.DB.prepare(
    "SELECT COALESCE(MAX(seq), 0) AS seq FROM messages WHERE conversation_id = ?1 AND hidden = 0",
  )
    .bind(conversationId)
    .first<{ seq: number }>();
  return row?.seq ?? 0;
}

export async function advanceCheckpoint(
  env: Env,
  conversationId: string,
  coveredTo: number,
  now: number,
): Promise<void> {
  await env.DB.prepare(
    `UPDATE conversations
     SET summarized_through = CASE WHEN summarized_through < ?2 THEN ?2 ELSE summarized_through END,
         updated_at = ?3
     WHERE id = ?1`,
  )
    .bind(conversationId, coveredTo, now)
    .run();
}

export async function enqueueSummaryJob(
  env: Env,
  conversation: ConversationRow,
  coveredTo: number,
  now: number,
): Promise<void> {
  const jobId = `summary:${conversation.id}:${coveredTo}`;
  await env.DB.batch([
    env.DB.prepare(
      `DELETE FROM summary_jobs
       WHERE conversation_id = ?1 AND status = 'pending' AND job_id <> ?2`,
    ).bind(conversation.id, jobId),
    env.DB.prepare(
      `INSERT INTO summary_jobs
         (job_id, conversation_id, owner_sub, covered_to, status, attempts, available_at,
          lease_until, error_code, created_at, updated_at)
       VALUES (?1, ?2, ?3, ?4, 'pending', 0, ?5, NULL, '', ?5, ?5)
       ON CONFLICT(job_id) DO UPDATE SET
         status = CASE WHEN summary_jobs.status = 'failed' AND summary_jobs.attempts < 3
                       THEN 'pending' ELSE summary_jobs.status END,
         available_at = CASE WHEN summary_jobs.status = 'failed' AND summary_jobs.attempts < 3
                             THEN excluded.available_at ELSE summary_jobs.available_at END,
         updated_at = ?5`,
    ).bind(jobId, conversation.id, conversation.owner_sub, coveredTo, now),
    env.DB.prepare(
      `UPDATE conversations SET summary_status = 'pending', summary_due_at = ?2
       WHERE id = ?1 AND summary_status <> 'running'`,
    ).bind(conversation.id, now),
  ]);
}

export async function getConversationById(env: Env, id: string): Promise<ConversationRow | null> {
  return env.DB.prepare(`SELECT ${CONVERSATION_COLUMNS} FROM conversations WHERE id = ?1`)
    .bind(id)
    .first<ConversationRow>();
}

export async function claimSummaryJob(
  env: Env,
  now: number,
  maxAttempts: number,
  leaseMs = 2 * 60 * 1000,
): Promise<SummaryJobRow | null> {
  const candidate = await env.DB.prepare(
    `SELECT job_id, conversation_id, owner_sub, covered_to, status, attempts,
            available_at, lease_until, error_code, created_at, updated_at
     FROM summary_jobs
     WHERE attempts < ?2
       AND (
         (status = 'pending' AND available_at <= ?1)
         OR (status = 'running' AND lease_until IS NOT NULL AND lease_until < ?1)
       )
     ORDER BY available_at ASC, created_at ASC
     LIMIT 1`,
  )
    .bind(now, maxAttempts)
    .first<SummaryJobRow>();
  if (!candidate) return null;
  const claimed = await env.DB.prepare(
    `UPDATE summary_jobs
     SET status = 'running', attempts = attempts + 1, lease_until = ?3, updated_at = ?2
     WHERE job_id = ?1 AND (
       (status = 'pending' AND available_at <= ?2)
       OR (status = 'running' AND lease_until IS NOT NULL AND lease_until < ?2)
     )
     RETURNING job_id, conversation_id, owner_sub, covered_to, status, attempts,
               available_at, lease_until, error_code, created_at, updated_at`,
  )
    .bind(candidate.job_id, now, now + leaseMs)
    .first<SummaryJobRow>();
  return claimed ?? null;
}

export async function markSummaryJob(
  env: Env,
  jobId: string,
  status: "pending" | "completed" | "failed",
  options: { availableAt?: number; error?: string; now?: number } = {},
): Promise<void> {
  const now = options.now ?? nowMs();
  await env.DB.prepare(
    `UPDATE summary_jobs
     SET status = ?2, available_at = COALESCE(?3, available_at),
         error_code = ?4, lease_until = NULL, updated_at = ?5
     WHERE job_id = ?1`,
  )
    .bind(jobId, status, options.availableAt ?? null, options.error ?? "", now)
    .run();
}

export async function getConversationSummary(
  env: Env,
  conversationId: string,
): Promise<SummaryRow | null> {
  return env.DB.prepare(
    `SELECT conversation_id, owner_sub, content, version, covered_from, covered_to,
            doc_key, index_status, index_error, created_at, updated_at, indexed_at
     FROM conversation_summaries WHERE conversation_id = ?1`,
  )
    .bind(conversationId)
    .first<SummaryRow>();
}

export async function saveConversationSummary(
  env: Env,
  value: {
    conversationId: string;
    ownerSub: string;
    content: string;
    coveredFrom: number;
    coveredTo: number;
    docKey: string;
    now: number;
  },
): Promise<{ applied: boolean; version: number }> {
  const row = await env.DB.prepare(
    `INSERT INTO conversation_summaries
       (conversation_id, owner_sub, content, version, covered_from, covered_to,
        doc_key, index_status, index_error, created_at, updated_at, indexed_at)
     VALUES (?1, ?2, ?3, 1, ?4, ?5, ?6, 'pending', '', ?7, ?7, NULL)
     ON CONFLICT(conversation_id) DO UPDATE SET
       content = excluded.content,
       version = conversation_summaries.version + 1,
       covered_from = excluded.covered_from,
       covered_to = excluded.covered_to,
       doc_key = excluded.doc_key,
       index_status = 'pending',
       index_error = '',
       updated_at = excluded.updated_at,
       indexed_at = NULL
     WHERE excluded.covered_to > conversation_summaries.covered_to
     RETURNING version, covered_to`,
  )
    .bind(
      value.conversationId,
      value.ownerSub,
      value.content,
      value.coveredFrom,
      value.coveredTo,
      value.docKey,
      value.now,
    )
    .first<{ version: number; covered_to: number }>();
  if (row) {
    await env.DB.prepare(
      `UPDATE conversations
       SET summarized_through = CASE WHEN summarized_through < ?2 THEN ?2 ELSE summarized_through END,
           summary_status = 'idle', summary_due_at = NULL, updated_at = ?3
       WHERE id = ?1`,
    )
      .bind(value.conversationId, value.coveredTo, value.now)
      .run();
    return { applied: true, version: row.version };
  }
  await env.DB.prepare(
    `UPDATE conversations
     SET summarized_through = CASE WHEN summarized_through < ?2 THEN ?2 ELSE summarized_through END,
         summary_status = 'idle', summary_due_at = NULL, updated_at = ?3
     WHERE id = ?1`,
  )
    .bind(value.conversationId, value.coveredTo, value.now)
    .run();
  return { applied: false, version: 0 };
}

export async function setConversationSummaryStatus(
  env: Env,
  conversationId: string,
  status: string,
  now: number,
): Promise<void> {
  await env.DB.prepare(
    `UPDATE conversations SET summary_status = ?2, summary_due_at = NULL, updated_at = ?3
     WHERE id = ?1`,
  )
    .bind(conversationId, status, now)
    .run();
}

export async function setSummaryIndexResult(
  env: Env,
  conversationId: string,
  status: "indexed" | "failed" | "unconfigured",
  error = "",
): Promise<void> {
  const now = nowMs();
  await env.DB.prepare(
    `UPDATE conversation_summaries
     SET index_status = ?2, index_error = ?3,
         indexed_at = CASE WHEN ?2 = 'indexed' THEN ?4 ELSE indexed_at END,
         updated_at = ?4
     WHERE conversation_id = ?1`,
  )
    .bind(conversationId, status, error.slice(0, 500), now)
    .run();
}

export async function pendingSummaryIndexes(env: Env, now: number, limit = 5): Promise<SummaryRow[]> {
  const result = await env.DB.prepare(
    `SELECT conversation_id, owner_sub, content, version, covered_from, covered_to,
            doc_key, index_status, index_error, created_at, updated_at, indexed_at
      FROM conversation_summaries
      WHERE index_status IN ('pending', 'failed') AND updated_at <= ?1
      ORDER BY updated_at ASC
      LIMIT ?2`,
  )
    .bind(now - 30_000, limit)
    .all<SummaryRow>();
  return result.results ?? [];
}

/* -------------------------------------------------------------------------- */
/* Channel daily summaries                                                    */
/* -------------------------------------------------------------------------- */

const CHANNEL_DAILY_COLUMNS =
  "id, owner_sub, conversation_id, summary_type, period_start, period_end, local_date, " +
  "content, version, covered_from_seq, covered_to_seq, source_summary_ids, " +
  "index_status, index_error, retry_count, source_revision, created_at, updated_at, indexed_at";

export interface ChannelDailySummaryRow {
  id: string;
  owner_sub: string;
  conversation_id: string;
  summary_type: string;
  period_start: string;
  period_end: string;
  local_date: string;
  content: string;
  version: number;
  covered_from_seq: number;
  covered_to_seq: number;
  source_summary_ids: string;
  index_status: string;
  index_error: string;
  retry_count: number;
  source_revision: string;
  created_at: number;
  updated_at: number;
  indexed_at: number | null;
}

export async function getChannelDailySummary(
  env: Env,
  ownerSub: string,
  conversationId: string,
  localDate: string,
): Promise<ChannelDailySummaryRow | null> {
  return env.DB.prepare(
    `SELECT ${CHANNEL_DAILY_COLUMNS} FROM channel_daily_summaries
      WHERE owner_sub = ?1 AND conversation_id = ?2 AND local_date = ?3
        AND summary_type = 'daily'
      LIMIT 1`,
  )
    .bind(ownerSub, conversationId, localDate)
    .first<ChannelDailySummaryRow>();
}

export async function saveChannelDailySummary(
  env: Env,
  value: {
    id: string;
    ownerSub: string;
    conversationId: string;
    localDate: string;
    periodStart: string;
    periodEnd: string;
    content: string;
    coveredFromSeq: number;
    coveredToSeq: number;
    sourceSummaryIds: string[];
    sourceRevision: string;
    generationStatus?: string;
    now: number;
  },
): Promise<{ applied: boolean; version: number; skipped: boolean }> {
  const now = value.now;
  const existing = await getChannelDailySummary(
    env,
    value.ownerSub,
    value.conversationId,
    value.localDate,
  );

  // Avoid regenerating unchanged summaries: same content + same coverage → skip.
  if (
    existing &&
    existing.content === value.content &&
    existing.covered_to_seq === value.coveredToSeq
  ) {
    return { applied: false, version: existing.version, skipped: true };
  }

  const generationStatus = value.generationStatus ?? "pending";
  const row = await env.DB.prepare(
    `INSERT INTO channel_daily_summaries
       (id, owner_sub, conversation_id, summary_type, period_start, period_end,
        local_date, content, version, covered_from_seq, covered_to_seq,
        source_summary_ids, index_status, index_error, retry_count,
        source_revision, created_at, updated_at)
      VALUES (?1, ?2, ?3, 'daily', ?4, ?5, ?6, ?7, 1, ?8, ?9, ?10, 'pending', '', 0,
        ?11, ?12, ?13)
      ON CONFLICT(owner_sub, conversation_id, summary_type, period_start) DO UPDATE SET
        content = excluded.content,
        version = channel_daily_summaries.version + 1,
        period_end = excluded.period_end,
        local_date = excluded.local_date,
        covered_from_seq = excluded.covered_from_seq,
        covered_to_seq = excluded.covered_to_seq,
        source_summary_ids = excluded.source_summary_ids,
        source_revision = excluded.source_revision,
        index_status = 'pending',
        index_error = '',
        retry_count = 0,
        updated_at = excluded.updated_at,
        indexed_at = NULL
      WHERE excluded.covered_to_seq > channel_daily_summaries.covered_to_seq
      RETURNING version`,
  )
    .bind(
      value.id,
      value.ownerSub,
      value.conversationId,
      value.periodStart,
      value.periodEnd,
      value.localDate,
      value.content,
      value.coveredFromSeq,
      value.coveredToSeq,
      JSON.stringify(value.sourceSummaryIds),
      value.sourceRevision,
      now,
      now,
    )
    .first<{ version: number }>();

  if (row) {
    if (generationStatus !== "pending") {
      await env.DB.prepare(
        `UPDATE channel_daily_summaries SET generation_status = ?2, updated_at = ?3
          WHERE id = ?1`,
      )
        .bind(value.id, generationStatus, now)
        .run();
    }
    return { applied: true, version: row.version, skipped: false };
  }
  // The conflict clause's WHERE blocked the update (coverage didn't extend).
  return { applied: false, version: existing?.version ?? 0, skipped: false };
}

export async function setChannelDailySummaryIndexStatus(
  env: Env,
  conversationId: string,
  localDate: string,
  status: "indexed" | "failed" | "unconfigured",
  error = "",
): Promise<void> {
  const now = Date.now();
  await env.DB.prepare(
    `UPDATE channel_daily_summaries
      SET index_status = ?3, index_error = ?4,
          indexed_at = CASE WHEN ?3 = 'indexed' THEN ?5 ELSE indexed_at END,
          updated_at = ?5
      WHERE conversation_id = ?1 AND local_date = ?2 AND summary_type = 'daily'`,
  )
    .bind(conversationId, localDate, status, error.slice(0, 500), now)
    .run();
}

export async function incrementChannelDailySummaryRetry(
  env: Env,
  conversationId: string,
  localDate: string,
): Promise<number> {
  const row = await env.DB.prepare(
    `UPDATE channel_daily_summaries
      SET retry_count = retry_count + 1, updated_at = ?3
      WHERE conversation_id = ?1 AND local_date = ?2 AND summary_type = 'daily'
      RETURNING retry_count`,
  )
    .bind(conversationId, localDate, Date.now())
    .first<{ retry_count: number }>();
  return row?.retry_count ?? 0;
}

export async function pendingChannelDailySummaryIndexes(
  env: Env,
  now: number,
  limit = 5,
): Promise<ChannelDailySummaryRow[]> {
  const result = await env.DB.prepare(
    `SELECT ${CHANNEL_DAILY_COLUMNS} FROM channel_daily_summaries
      WHERE index_status IN ('pending', 'failed')
        AND retry_count < 5
        AND updated_at <= ?1
      ORDER BY updated_at ASC
      LIMIT ?2`,
  )
    .bind(now - 30_000, limit)
    .all<ChannelDailySummaryRow>();
  return result.results ?? [];
}

export async function listMessagesInSeqRange(
  env: Env,
  ownerSub: string,
  conversationId: string,
  fromSeq: number,
  toSeq: number,
): Promise<MessageRow[]> {
  const result = await env.DB.prepare(
    `SELECT ${MESSAGE_COLUMNS} FROM messages
      WHERE conversation_id = ?1 AND owner_sub = ?2
        AND seq >= ?3 AND seq <= ?4
        AND hidden = 0 AND role IN ('user','assistant')
      ORDER BY seq ASC`,
  )
    .bind(conversationId, ownerSub, fromSeq, toSeq)
    .all<MessageRow>();
  return result.results ?? [];
}

/* -------------------------------------------------------------------------- */
/* Channel summaries (daily / monthly / yearly)                               */
/* -------------------------------------------------------------------------- */

export async function getChannelSummary(
  env: Env,
  ownerSub: string,
  conversationId: string,
  summaryType: string,
  periodStart: string,
): Promise<ChannelDailySummaryRow | null> {
  return env.DB.prepare(
    `SELECT ${CHANNEL_DAILY_COLUMNS} FROM channel_daily_summaries
      WHERE owner_sub = ?1 AND conversation_id = ?2 AND summary_type = ?3 AND period_start = ?4
      LIMIT 1`,
  )
    .bind(ownerSub, conversationId, summaryType, periodStart)
    .first<ChannelDailySummaryRow>();
}

export async function saveChannelSummary(
  env: Env,
  value: {
    id: string;
    ownerSub: string;
    conversationId: string;
    summaryType: string;
    periodStart: string;
    periodEnd: string;
    localDate: string;
    content: string;
    coveredFromSeq: number;
    coveredToSeq: number;
    sourceSummaryIds: string[];
    sourceRevision: string;
    generationStatus?: string;
    now: number;
  },
): Promise<{ applied: boolean; version: number; skipped: boolean }> {
  const now = value.now;
  const existing = await getChannelSummary(
    env,
    value.ownerSub,
    value.conversationId,
    value.summaryType,
    value.periodStart,
  );

  if (
    existing &&
    existing.content === value.content &&
    existing.covered_to_seq === value.coveredToSeq
  ) {
    return { applied: false, version: existing.version, skipped: true };
  }

  const generationStatus = value.generationStatus ?? "pending";
  const row = await env.DB.prepare(
    `INSERT INTO channel_daily_summaries
       (id, owner_sub, conversation_id, summary_type, period_start, period_end,
        local_date, content, version, covered_from_seq, covered_to_seq,
        source_summary_ids, index_status, index_error, retry_count,
        source_revision, created_at, updated_at)
     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, 1, ?9, ?10, ?11, 'pending', '', 0,
       ?12, ?13, ?14, ?14)
     ON CONFLICT(owner_sub, conversation_id, summary_type, period_start) DO UPDATE SET
       content = excluded.content,
       version = channel_daily_summaries.version + 1,
       period_end = excluded.period_end,
       local_date = excluded.local_date,
       covered_from_seq = excluded.covered_from_seq,
       covered_to_seq = excluded.covered_to_seq,
       source_summary_ids = excluded.source_summary_ids,
       source_revision = excluded.source_revision,
       index_status = 'pending',
       index_error = '',
       retry_count = 0,
       updated_at = excluded.updated_at,
       indexed_at = NULL
     WHERE excluded.covered_to_seq > channel_daily_summaries.covered_to_seq
     RETURNING version`,
  )
    .bind(
      value.id,
      value.ownerSub,
      value.conversationId,
      value.summaryType,
      value.periodStart,
      value.periodEnd,
      value.localDate,
      value.content,
      value.coveredFromSeq,
      value.coveredToSeq,
      JSON.stringify(value.sourceSummaryIds),
      value.sourceRevision,
      now,
      now,
    )
    .first<{ version: number }>();

  if (row) {
    if (generationStatus !== "pending") {
      await env.DB.prepare(
        `UPDATE channel_daily_summaries SET generation_status = ?2, updated_at = ?3
          WHERE id = ?1`,
      )
        .bind(value.id, generationStatus, now)
        .run();
    }
    return { applied: true, version: row.version, skipped: false };
  }
  return { applied: false, version: existing?.version ?? 0, skipped: false };
}

/** Mark every active summary whose id is listed as compacted (absorbed into a
 * higher-level summary).  Compacted rows stay in D1 for audit but leave the
 * active set and the canonical RAG document. */
export async function markChannelSummariesCompacted(
  env: Env,
  ids: string[],
  now: number,
): Promise<number> {
  if (ids.length === 0) return 0;
  const placeholders = ids.map((_, i) => `?${i + 2}`).join(",");
  const result = await env.DB.prepare(
    `UPDATE channel_daily_summaries
      SET compacted = 1, updated_at = ?1
      WHERE id IN (${placeholders})`,
  )
    .bind(now, ...ids)
    .run();
  return (result.meta.changes ?? 0);
}

/** Active summaries (successful, not compacted) for a channel, oldest first. */
export async function listActiveChannelSummaries(
  env: Env,
  ownerSub: string,
  conversationId: string,
): Promise<ChannelDailySummaryRow[]> {
  const result = await env.DB.prepare(
    `SELECT ${CHANNEL_DAILY_COLUMNS} FROM channel_daily_summaries
      WHERE owner_sub = ?1 AND conversation_id = ?2
        AND generation_status = 'success'
        AND compacted = 0
      ORDER BY period_start ASC,
               CASE summary_type WHEN 'yearly' THEN 1 WHEN 'monthly' THEN 2 ELSE 3 END,
               local_date ASC`,
  )
    .bind(ownerSub, conversationId)
    .all<ChannelDailySummaryRow>();
  return result.results ?? [];
}

/** Successful daily summaries for a calendar month that are still active. */
export async function listDailySummariesForMonth(
  env: Env,
  ownerSub: string,
  conversationId: string,
  yearMonth: string, // '2026-10'
): Promise<ChannelDailySummaryRow[]> {
  const result = await env.DB.prepare(
    `SELECT ${CHANNEL_DAILY_COLUMNS} FROM channel_daily_summaries
      WHERE owner_sub = ?1 AND conversation_id = ?2
        AND summary_type = 'daily'
        AND generation_status = 'success'
        AND compacted = 0
        AND local_date LIKE ?3 || '-%'
      ORDER BY local_date ASC`,
  )
    .bind(ownerSub, conversationId, yearMonth)
    .all<ChannelDailySummaryRow>();
  return result.results ?? [];
}

/** Successful monthly summaries for a calendar year that are still active. */
export async function listMonthlySummariesForYear(
  env: Env,
  ownerSub: string,
  conversationId: string,
  year: string, // '2026'
): Promise<ChannelDailySummaryRow[]> {
  const result = await env.DB.prepare(
    `SELECT ${CHANNEL_DAILY_COLUMNS} FROM channel_daily_summaries
      WHERE owner_sub = ?1 AND conversation_id = ?2
        AND summary_type = 'monthly'
        AND generation_status = 'success'
        AND compacted = 0
        AND period_start LIKE ?3 || '-%'
      ORDER BY period_start ASC`,
  )
    .bind(ownerSub, conversationId, year)
    .all<ChannelDailySummaryRow>();
  return result.results ?? [];
}

export async function setChannelSummaryGenerationStatus(
  env: Env,
  ownerSub: string,
  conversationId: string,
  summaryType: string,
  periodStart: string,
  status: string,
  now: number,
): Promise<void> {
  await env.DB.prepare(
    `UPDATE channel_daily_summaries
      SET generation_status = ?5, updated_at = ?6
      WHERE owner_sub = ?1 AND conversation_id = ?2 AND summary_type = ?3 AND period_start = ?4`,
  )
    .bind(ownerSub, conversationId, summaryType, periodStart, status, now)
    .run();
}

export async function setChannelSummaryIndexStatusById(
  env: Env,
  id: string,
  status: "indexed" | "failed" | "unconfigured",
  error = "",
): Promise<void> {
  const now = Date.now();
  await env.DB.prepare(
    `UPDATE channel_daily_summaries
      SET index_status = ?2, index_error = ?3,
          indexed_at = CASE WHEN ?2 = 'indexed' THEN ?4 ELSE indexed_at END,
          updated_at = ?4
      WHERE id = ?1`,
  )
    .bind(id, status, error.slice(0, 500), now)
    .run();
}

export async function incrementChannelSummaryRetryById(env: Env, id: string): Promise<number> {
  const row = await env.DB.prepare(
    `UPDATE channel_daily_summaries
      SET retry_count = retry_count + 1, updated_at = ?2
      WHERE id = ?1
      RETURNING retry_count`,
  )
    .bind(id, Date.now())
    .first<{ retry_count: number }>();
  return row?.retry_count ?? 0;
}

export function mapConversation(row: ConversationRow): Record<string, unknown> {
  return {
    thread_id: row.id,
    id: row.id,
    user_id: row.owner_id,
    title: row.title,
    created_at: Math.floor(row.created_at / 1000),
    updated_at: Math.floor(row.updated_at / 1000),
    summary_status: row.summary_status,
    summarized_through: row.summarized_through,
  };
}

export function mapMessage(row: MessageRow): Record<string, unknown> {
  return {
    id: row.id,
    thread_id: row.conversation_id,
    role: row.role,
    agent: row.agent,
    content: row.content,
    data: row.data,
    seq: row.seq,
    created_at: Math.floor(row.created_at / 1000),
    request_id: row.request_id,
  };
}
