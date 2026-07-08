import { config } from '../config.js';
import { createLogger } from '../logger.js';
import type { AgentBackend } from './types.js';
import { ClaudeBackend } from './claude.js';
import { GeminiBackend } from './gemini.js';
import { OllamaBackend } from './ollama.js';
import { DeepseekBackend } from './deepseek.js';

const log = createLogger('backend');

let backend: AgentBackend | null = null;

const backends: Record<typeof config.agent.provider, () => AgentBackend> = {
  claude: () => new ClaudeBackend(),
  gemini: () => new GeminiBackend(),
  ollama: () => new OllamaBackend(),
  deepseek: () => new DeepseekBackend(),
};

/** The active agent backend, selected by AGENT_PROVIDER. Constructed once. */
export function getBackend(): AgentBackend {
  if (backend) return backend;
  backend = backends[config.agent.provider]();
  log.info(`agent backend: ${backend.name}`);
  return backend;
}

export * from './types.js';
