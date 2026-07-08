import type { PermissionResult } from '@anthropic-ai/claude-agent-sdk';

/** Re-exported so callers don't need to import from the Claude SDK directly. */
export type { PermissionResult };

export type CanUseTool = (
  toolName: string,
  input: Record<string, unknown>,
) => Promise<PermissionResult>;

export interface RunAgentOptions {
  prompt: string;
  cwd: string;
  systemPrompt: string;
  canUseTool: CanUseTool;
  /** Called for each tool the agent invokes, for streaming progress. */
  onToolUse?: (name: string, input: Record<string, unknown>) => void;
  /** Called for each chunk of assistant text. */
  onText?: (text: string) => void;
}

export interface AgentResult {
  text: string | null;
  turns: number | undefined;
  costUsd: number | undefined;
  ok: boolean;
}

/**
 * A model-agnostic agent runtime: takes a goal + a permission gate and drives
 * a tool-use loop to completion. Implemented per provider (Claude SDK, Gemini
 * API). The orchestration around it (queue, telegram, autofix) is unaware of
 * which backend is active.
 */
export interface AgentBackend {
  readonly name: string;
  run(opts: RunAgentOptions): Promise<AgentResult>;
}

/** Tools that only read/inspect — safe to run without asking. */
export const READ_ONLY_TOOLS = new Set([
  'Read', 'Grep', 'Glob', 'WebFetch', 'WebSearch', 'TodoWrite', 'NotebookRead',
]);

/** Keys under which a tool input carries a filesystem path. */
export const PATH_KEYS = ['file_path', 'path', 'notebook_path'];
