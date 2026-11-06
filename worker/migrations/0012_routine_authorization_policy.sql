-- Scheduled-email authorization policy, persisted with each routine.
--
-- authorization_policy governs how a routine's email sends are authorized:
--   per_run        - every scheduled email requires an owner Allow/Deny approval (default; current behavior)
--   setup_only     - a one-time setup approval is required; later runs still respect owner isolation
--   owner_authorized - a narrowly scoped owner authorization covers the routine's runs
--
-- Scheduled work must not silently bypass direct email approval protections:
-- per_run is the default so no existing routine changes behavior.
ALTER TABLE routines ADD COLUMN authorization_policy TEXT NOT NULL DEFAULT 'per_run';

CREATE INDEX IF NOT EXISTS idx_routines_auth_policy ON routines(authorization_policy);
