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

export type ProviderEvent =
  | { type: "text"; text: string }
  | { type: "tool_call"; call: ToolCall }
  | { type: "done"; stopReason: string };

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
