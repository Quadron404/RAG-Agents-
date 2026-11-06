-- Agent identity, assignment, and execution tables for the dynamic specialist
-- workforce.  A specialist's permanent identity lives in agent_identities and
-- survives task completion.  Each assignment has its own task prompt, context,
-- expected deliverable, and output schema in agent_assignments.  Executions are
-- recorded in agent_executions so historical results stay reproducible.
--
-- Ownership: every row is owner_scoped by owner_sub (Google subject) so one
-- specialist can never be read or assigned by another owner.

-- Permanent specialist identity.  agent_id is stable and never reused.
CREATE TABLE IF NOT EXISTS agent_identities (
    agent_id          TEXT PRIMARY KEY,
    owner_sub         TEXT NOT NULL,
    owner_id          TEXT NOT NULL,
    name              TEXT NOT NULL DEFAULT '',
    slug              TEXT NOT NULL DEFAULT '',
    role              TEXT NOT NULL DEFAULT 'specialist',
    description       TEXT NOT NULL DEFAULT '',
    specialization    TEXT NOT NULL DEFAULT '',
    capability_profile TEXT NOT NULL DEFAULT '{}',
    identity_version  INTEGER NOT NULL DEFAULT 1,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_identities_owner ON agent_identities(owner_sub, owner_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_identities_slug ON agent_identities(owner_sub, slug);

-- One assignment per task.  A new assignment writes only task fields; it never
-- overwrites identity columns.  Identity updates increment identity_version and
-- apply only to future assignments.
CREATE TABLE IF NOT EXISTS agent_assignments (
    assignment_id     TEXT PRIMARY KEY,
    agent_id          TEXT NOT NULL,
    owner_sub         TEXT NOT NULL,
    owner_id          TEXT NOT NULL,
    task_prompt       TEXT NOT NULL DEFAULT '',
    context           TEXT NOT NULL DEFAULT '{}',
    expected_deliverable TEXT NOT NULL DEFAULT '',
    output_schema     TEXT NOT NULL DEFAULT '{}',
    status            TEXT NOT NULL DEFAULT 'pending',
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_assignments_agent ON agent_assignments(agent_id, status);
CREATE INDEX IF NOT EXISTS idx_agent_assignments_owner ON agent_assignments(owner_sub, created_at DESC);

-- One row per execution of an assignment.  Survives restarts and retries.
CREATE TABLE IF NOT EXISTS agent_executions (
    execution_id      TEXT PRIMARY KEY,
    assignment_id     TEXT NOT NULL,
    agent_id          TEXT NOT NULL,
    owner_sub         TEXT NOT NULL,
    identity_version  INTEGER NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',
    result            TEXT,
    error_code        TEXT,
    started_at        INTEGER,
    completed_at      INTEGER,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_executions_assignment ON agent_executions(assignment_id);
CREATE INDEX IF NOT EXISTS idx_agent_executions_owner ON agent_executions(owner_sub, created_at DESC);
