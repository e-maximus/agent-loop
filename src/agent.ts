// Thin facade over the pluggable agent backends. Callers (worker, autofix,
// answer) import runAgent + the shared types/constants from here and stay
// unaware of which provider (Gemini / Claude) is active.
import { getBackend } from './backends/index.js';
import type { AgentResult, RunAgentOptions } from './backends/types.js';

export {
  READ_ONLY_TOOLS,
  PATH_KEYS,
  type CanUseTool,
  type RunAgentOptions,
  type AgentResult,
  type AgentBackend,
  type PermissionResult,
} from './backends/types.js';

/** Runs one agent turn-loop to completion using the configured backend. */
export function runAgent(opts: RunAgentOptions): Promise<AgentResult> {
  return getBackend().run(opts);
}
