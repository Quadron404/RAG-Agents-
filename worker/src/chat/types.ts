/** Provider-neutral chat shapes shared by the Worker's models and tools. */

export interface ToolCall {
  id: string;
  name: string;
  arguments: string;
}

export interface LlmMessage {
  role: "system" | "user" | "assistant" | "tool";
  content: string;
  toolCalls?: ToolCall[];
  toolCallId?: string;
  name?: string;
}

export interface ToolDefinition {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
}

/** Typed stop reasons mapped from provider finish reasons. */
export type StopReason =
  | "stop"
  | "tool_calls"
  | "length"
  | "refusal"
  | "error"
  | "cancel"
  | string;

export interface Usage {
  promptTokens?: number;
  completionTokens?: number;
  totalTokens?: number;
}

/** Provider-neutral event stream emitted by every model adapter. */
export type ProviderEvent =
  | { type: "text"; text: string }
  | { type: "tool_call"; call: ToolCall }
  | { type: "done"; stopReason: StopReason; usage?: Usage }
  | { type: "error"; message: string; retryable?: boolean; status?: number }
  | { type: "cancel" };

export interface ModelProvider {
  name: string;
  model: string;
  stream(messages: LlmMessage[], tools: ToolDefinition[], signal?: AbortSignal): AsyncGenerator<ProviderEvent>;
}

export interface ProviderSelection {
  provider: ModelProvider;
  model: string;
}

export class ProviderError extends Error {
  status: number;
  retryable: boolean;

  constructor(message: string, status = 0, retryable = false) {
    super(message);
    this.name = "ProviderError";
    this.status = status;
    this.retryable = retryable;
  }
}
