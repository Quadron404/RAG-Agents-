/**
 * Minimal JSON Schema validator for tool arguments.
 *
 * Supports the subset of JSON Schema used by Worker tool definitions:
 * type, properties, required, additionalProperties, enum.
 *
 * Returns { ok: true } when the value conforms, or { ok: false, error }
 * describing the first violation.
 */
export interface SchemaViolation {
  path: string;
  message: string;
}

export function validateAgainstSchema(
  value: unknown,
  schema: Record<string, unknown>,
  path = "",
): { ok: true } | { ok: false; violations: SchemaViolation[] } {
  const violations: SchemaViolation[] = [];

  const type = schema.type as string | undefined;
  if (type) {
    const actual = typeOf(value);
    if (actual !== type) {
      violations.push({ path: path || "root", message: `expected ${type}, got ${actual}` });
      return { ok: false, violations };
    }
  }

  if (type === "object" && value && typeof value === "object" && !Array.isArray(value)) {
    const properties = schema.properties as Record<string, unknown> | undefined;
    const required = schema.required as string[] | undefined;
    const additional = schema.additionalProperties;

    if (required) {
      for (const key of required) {
        if (!(key in (value as Record<string, unknown>))) {
          violations.push({ path: `${path || "root"}.${key}`, message: "required property is missing" });
        }
      }
    }

    if (properties) {
      const obj = value as Record<string, unknown>;
      for (const [key, propSchema] of Object.entries(properties)) {
        if (key in obj) {
          const result = validateAgainstSchema(obj[key], propSchema as Record<string, unknown>, `${path || "root"}.${key}`);
          if (!result.ok) violations.push(...result.violations);
        }
      }
    }

    if (additional === false) {
      const obj = value as Record<string, unknown>;
      const known = properties ? Object.keys(properties) : [];
      for (const key of Object.keys(obj)) {
        if (!known.includes(key)) {
          violations.push({ path: `${path || "root"}.${key}`, message: "additional properties are not allowed" });
        }
      }
    }
  }

  if (type === "string" && typeof value === "string") {
    const enumValues = schema.enum as string[] | undefined;
    if (enumValues && !enumValues.includes(value)) {
      violations.push({ path: path || "root", message: `value is not one of ${enumValues.join(", ")}` });
    }
  }

  if (violations.length > 0) return { ok: false, violations };
  return { ok: true };
}

function typeOf(value: unknown): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  return typeof value;
}

export function validateToolArguments(
  call: { name: string; arguments: string },
  schema: Record<string, unknown> | null | undefined,
): { ok: true; args: Record<string, unknown> } | { ok: false; error: string } {
  if (!schema) {
    // No schema defined -- accept whatever parses as JSON.
    try {
      const parsed = JSON.parse(call.arguments || "{}");
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        return { ok: false, error: "tool arguments must be a JSON object" };
      }
      return { ok: true, args: parsed };
    } catch {
      return { ok: false, error: "tool arguments are not valid JSON" };
    }
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(call.arguments || "{}");
  } catch {
    return { ok: false, error: "tool arguments are not valid JSON" };
  }

  const result = validateAgainstSchema(parsed, schema);
  if (!result.ok) {
    const detail = result.violations.map((v) => `${v.path}: ${v.message}`).join("; ");
    return { ok: false, error: `invalid tool arguments: ${detail}` };
  }

  return { ok: true, args: parsed as Record<string, unknown> };
}
