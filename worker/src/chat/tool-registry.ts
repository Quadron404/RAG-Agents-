/**
 * Centralized tool registry and runtime schema validation (PROMPT-39, PROMPT-40).
 *
 * Every tool exposed to Chief across execution boundaries (Worker-native chat)
 * is registered here once with: name, JSON Schema, owner scope, side-effect
 * classification, approval requirement, timeout, and retry safety.  The
 * registry is the single source of truth; no tool may be executed unless it is
 * registered here and its arguments validate against its schema.
 *
 * Side-effect classification:
 *   * none        - read-only, no external effect (history, list_routines)
 *   * stateful    - mutates D1 only (create/pause/resume/delete routine)
 *   * external    - triggers an external side effect behind approval (email)
 *
 * Approval requirement:
 *   * none   - executes immediately
 *   * gate   - creates a pending approval; owner must Allow/Deny
 */
import type { ToolCall, ToolDefinition } from "./types";

export type SideEffect = "none" | "stateful" | "external";
export type ApprovalPolicy = "none" | "gate";

export interface ToolPolicy {
  /** Human-readable description of what the tool does. */
  description: string;
  /** JSON Schema for argument validation. */
  schema: Record<string, unknown>;
  /** Owner scope: all calls are bound to the authenticated session's owner. */
  ownerScoped: boolean;
  /** Whether the tool has side effects beyond returning data. */
  sideEffect: SideEffect;
  /** Whether execution requires owner approval before side effects occur. */
  approval: ApprovalPolicy;
  /** Maximum execution time in ms. */
  timeoutMs: number;
  /** Whether a transient failure may be retried safely (idempotent). */
  retryable: boolean;
}

export interface RegisteredTool extends ToolDefinition {
  policy: ToolPolicy;
}

/** Validation error returned to Chief so it can correct safe mistakes. */
export interface ToolValidationError {
  code: "invalid_arguments" | "unknown_tool" | "schema_violation";
  message: string;
  path?: string;
}

/* -------------------------------------------------------------------------- */
/* Minimal JSON Schema validator (subset: type, required, properties,          */
/* additionalProperties, enum, minimum, maximum, minLength, maxLength).        */
/* No external dependency is introduced.                                      */
/* -------------------------------------------------------------------------- */

function validateType(value: unknown, type: string): string[] {
  const errors: string[] = [];
  switch (type) {
    case "string":
      if (typeof value !== "string") errors.push("expected string");
      break;
    case "number":
      if (typeof value !== "number" || !Number.isFinite(value)) errors.push("expected number");
      break;
    case "integer":
      if (!Number.isInteger(value)) errors.push("expected integer");
      break;
    case "boolean":
      if (typeof value !== "boolean") errors.push("expected boolean");
      break;
    case "object":
      if (typeof value !== "object" || value === null || Array.isArray(value)) errors.push("expected object");
      break;
    case "array":
      if (!Array.isArray(value)) errors.push("expected array");
      break;
    case "null":
      if (value !== null) errors.push("expected null");
      break;
  }
  return errors;
}

function validateSchema(
  schema: Record<string, unknown>,
  value: unknown,
  path: string,
  errors: string[],
): void {
  const type = schema.type;
  if (type) {
    if (Array.isArray(type)) {
      if (!type.some((t) => validateType(value, t as string).length === 0)) {
        errors.push(`${path}: expected one of [${type.join(", ")}]`);
      }
    } else {
      const tErrors = validateType(value, type as string);
      if (tErrors.length) errors.push(`${path}: ${tErrors.join("; ")}`);
    }
  }

  if (value === null || value === undefined) return;

  const enumValues = schema.enum;
  if (Array.isArray(enumValues) && !enumValues.includes(value)) {
    errors.push(`${path}: value not in enum [${enumValues.join(", ")}]`);
  }

  if (typeof value === "string") {
    if (schema.minLength !== undefined && value.length < Number(schema.minLength)) {
      errors.push(`${path}: string shorter than minLength ${schema.minLength}`);
    }
    if (schema.maxLength !== undefined && value.length > Number(schema.maxLength)) {
      errors.push(`${path}: string longer than maxLength ${schema.maxLength}`);
    }
  }

  if (typeof value === "number") {
    if (schema.minimum !== undefined && value < Number(schema.minimum)) {
      errors.push(`${path}: number below minimum ${schema.minimum}`);
    }
    if (schema.maximum !== undefined && value > Number(schema.maximum)) {
      errors.push(`${path}: number above maximum ${schema.maximum}`);
    }
  }

  if (typeof value === "object" && !Array.isArray(value)) {
    const obj = value as Record<string, unknown>;
    const required = schema.required;
    if (Array.isArray(required)) {
      for (const req of required) {
        if (!(req in obj)) errors.push(`${path}.${req}: required property missing`);
      }
    }

    const properties = schema.properties;
    if (properties && typeof properties === "object") {
      const props = properties as Record<string, Record<string, unknown>>;
      for (const [key, propSchema] of Object.entries(props)) {
        if (key in obj) {
          validateSchema(propSchema, obj[key], `${path}.${key}`, errors);
        }
      }
    }

    const additional = schema.additionalProperties;
    if (additional === false) {
      const known = new Set(Object.keys(properties || {}));
      for (const key of Object.keys(obj)) {
        if (!known.has(key)) {
          errors.push(`${path}.${key}: unknown property not allowed`);
        }
      }
    }
  }
}

/** Validate a tool call's arguments against its registered schema. */
export function validateToolArguments(
  tool: RegisteredTool,
  call: ToolCall,
): { ok: true; arguments: Record<string, unknown> } | { ok: false; error: ToolValidationError } {
  const parsed: unknown = call.arguments ? JSON.parse(call.arguments) : {};
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return {
      ok: false,
      error: { code: "schema_violation", message: "arguments must be a JSON object", path: "arguments" },
    };
  }

  const errors: string[] = [];
  validateSchema(tool.parameters, parsed as Record<string, unknown>, "arguments", errors);
  if (errors.length) {
    return {
      ok: false,
      error: {
        code: "schema_violation",
        message: `argument validation failed: ${errors.join("; ")}`,
      },
    };
  }

  return { ok: true, arguments: parsed as Record<string, unknown> };
}

/* -------------------------------------------------------------------------- */
/* Tool registry                                                             */
/* -------------------------------------------------------------------------- */

function makeTool(
  name: string,
  description: string,
  schema: Record<string, unknown>,
  policy: ToolPolicy,
): RegisteredTool {
  return {
    name,
    description,
    parameters: schema,
    policy,
  };
}

// --- History tool (Worker-native, read-only) -------------------------------

export const HISTORY_TOOL_REGISTRY: RegisteredTool = makeTool(
  "tool_call_history",
  "Search summaries of this user's earlier conversations. Use only when the current conversation lacks needed information.",
  {
    type: "object",
    properties: {
      query: { type: "string", description: "A precise search query", minLength: 1, maxLength: 500 },
    },
    required: ["query"],
    additionalProperties: false,
  },
  {
    description: "Searches conversation summaries owned by the authenticated user.",
    schema: {},
    ownerScoped: true,
    sideEffect: "none",
    approval: "none",
    timeoutMs: 10_000,
    retryable: true,
  },
);

// --- Routine tools (Worker-native, stateful) ------------------------------

export const ROUTINE_TOOLS_REGISTRY: RegisteredTool[] = [
  makeTool(
    "create_routine",
    "Schedule a recurring task that runs automatically, even while offline.",
    {
      type: "object",
      properties: {
        name: { type: "string", minLength: 1, maxLength: 200 },
        task_prompt: { type: "string", minLength: 1 },
        schedule_type: { type: "string", enum: ["once", "daily", "monthly"] },
        time: { type: ["string", "null"] },
        at: { type: ["string", "null"] },
        day_of_month: { type: ["integer", "null"], minimum: 1, maximum: 28 },
        timezone: { type: "string" },
      },
      required: ["name", "task_prompt", "schedule_type"],
      additionalProperties: false,
    },
    {
      description: "Creates a durable routine owned by the authenticated user.",
      schema: {},
      ownerScoped: true,
      sideEffect: "stateful",
      approval: "none",
      timeoutMs: 5_000,
      retryable: true,
    },
  ),
  makeTool(
    "list_routines",
    "List the routines you have scheduled, with their next run and status.",
    { type: "object", properties: {}, additionalProperties: false },
    {
      description: "Lists routines owned by the authenticated user.",
      schema: {},
      ownerScoped: true,
      sideEffect: "none",
      approval: "none",
      timeoutMs: 5_000,
      retryable: true,
    },
  ),
  makeTool(
    "get_routine",
    "Get one routine by id, including its full instructions and run history.",
    {
      type: "object",
      properties: { routine_id: { type: "string", minLength: 1 } },
      required: ["routine_id"],
      additionalProperties: false,
    },
    {
      description: "Reads a routine owned by the authenticated user.",
      schema: {},
      ownerScoped: true,
      sideEffect: "none",
      approval: "none",
      timeoutMs: 5_000,
      retryable: true,
    },
  ),
  makeTool(
    "pause_routine",
    "Pause a routine so it starts no new runs until resumed.",
    {
      type: "object",
      properties: { routine_id: { type: "string", minLength: 1 } },
      required: ["routine_id"],
      additionalProperties: false,
    },
    {
      description: "Updates a routine owned by the authenticated user.",
      schema: {},
      ownerScoped: true,
      sideEffect: "stateful",
      approval: "none",
      timeoutMs: 5_000,
      retryable: true,
    },
  ),
  makeTool(
    "resume_routine",
    "Resume a paused routine and recompute its next run.",
    {
      type: "object",
      properties: { routine_id: { type: "string", minLength: 1 } },
      required: ["routine_id"],
      additionalProperties: false,
    },
    {
      description: "Updates a routine owned by the authenticated user.",
      schema: {},
      ownerScoped: true,
      sideEffect: "stateful",
      approval: "none",
      timeoutMs: 5_000,
      retryable: true,
    },
  ),
  makeTool(
    "delete_routine",
    "Delete a routine permanently, including its run history.",
    {
      type: "object",
      properties: { routine_id: { type: "string", minLength: 1 } },
      required: ["routine_id"],
      additionalProperties: false,
    },
    {
      description: "Deletes a routine owned by the authenticated user.",
      schema: {},
      ownerScoped: true,
      sideEffect: "stateful",
      approval: "none",
      timeoutMs: 5_000,
      retryable: false,
    },
  ),
];

// --- Mail / connector tools (external, approval-gated) --------------------

export const MAIL_TOOL_CALL_REGISTRY: RegisteredTool = makeTool(
  "mail_tool_call",
  "Send an email to yourself through the central bot mailbox. Requires owner approval before sending.",
  {
    type: "object",
    properties: { content: { type: "string", minLength: 1, maxLength: 100_000 } },
    required: ["content"],
    additionalProperties: false,
  },
  {
    description: "Sends an email via the central Gmail sender to the owner's verified address.",
    schema: {},
    ownerScoped: true,
    sideEffect: "external",
    approval: "gate",
    timeoutMs: 30_000,
    retryable: false,
  },
);

export const GMAIL_SEND_EMAIL_REGISTRY: RegisteredTool = makeTool(
  "gmail.send_email",
  "Send an email to yourself through the central bot mailbox. Requires owner approval before sending.",
  {
    type: "object",
    properties: { content: { type: "string", minLength: 1, maxLength: 100_000 } },
    required: ["content"],
    additionalProperties: false,
  },
  {
    description: "Sends an email via the central Gmail sender to the owner's verified address.",
    schema: {},
    ownerScoped: true,
    sideEffect: "external",
    approval: "gate",
    timeoutMs: 30_000,
    retryable: false,
  },
);

/** All chat-exposed tools, keyed by name. This is the authoritative registry. */
export const CHAT_TOOL_REGISTRY: Map<string, RegisteredTool> = (() => {
  const map = new Map<string, RegisteredTool>();
  const register = (tool: RegisteredTool) => map.set(tool.name, tool);
  register(HISTORY_TOOL_REGISTRY);
  for (const t of ROUTINE_TOOLS_REGISTRY) register(t);
  register(MAIL_TOOL_CALL_REGISTRY);
  register(GMAIL_SEND_EMAIL_REGISTRY);
  return map;
})();

/** Look up a tool by name. Returns undefined when the tool is not registered. */
export function getRegisteredTool(name: string): RegisteredTool | undefined {
  return CHAT_TOOL_REGISTRY.get(name);
}

/** Return all registered tool definitions (for provider tool declarations). */
export function getRegisteredToolDefinitions(): ToolDefinition[] {
  return [...CHAT_TOOL_REGISTRY.values()].map((t) => ({
    name: t.name,
    description: t.description,
    parameters: t.parameters,
  }));
}

/** Return all tools whose side effect is external and require approval. */
export function getApprovalGatedTools(): RegisteredTool[] {
  return [...CHAT_TOOL_REGISTRY.values()].filter((t) => t.policy.approval === "gate");
}
