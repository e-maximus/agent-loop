import 'dotenv/config';
import { z } from 'zod';
import path from 'node:path';
import fs from 'node:fs';
import { parse as parseYaml } from 'yaml';
import { rootConfigSchema, type SourceConfig } from './sources/types.js';

// ── Global env: agent backend, DB, and secrets. Source definitions live in the
// YAML config file (see loadSources); secrets there are referenced as ${VAR}
// and resolved from this same environment. ──
const envSchema = z.object({
  // Optional: if unset, the Agent SDK falls back to your Claude Code login
  // (Pro/Max subscription) for auth.
  ANTHROPIC_API_KEY: z.string().default(''),
  // Which agent backend powers task execution.
  AGENT_PROVIDER: z.enum(['gemini', 'claude', 'ollama', 'deepseek']).default('deepseek'),
  AGENT_WORKSPACE_ROOT: z.string().min(1).default(process.cwd()),
  AGENT_MAX_TURNS: z.coerce.number().int().positive().default(40),

  // Claude backend (used when AGENT_PROVIDER=claude).
  AGENT_MODEL: z.string().default('claude-opus-4-8'),

  // Gemini backend (used when AGENT_PROVIDER=gemini).
  GEMINI_API_KEY: z.string().default(''),
  GEMINI_MODEL: z.string().default('gemini-2.5-pro'),

  // Ollama backend (used when AGENT_PROVIDER=ollama). Runs against a local
  // Ollama server — no API key, no per-second rate limits.
  OLLAMA_BASE_URL: z.string().default('http://localhost:11434'),
  OLLAMA_MODEL: z.string().default('qwen3:8b'),

  // DeepSeek backend (used when AGENT_PROVIDER=deepseek). Cloud API, key from
  // https://platform.deepseek.com. OpenAI-compatible endpoint.
  DEEPSEEK_API_KEY: z.string().default(''),
  DEEPSEEK_BASE_URL: z.string().default('https://api.deepseek.com'),
  DEEPSEEK_MODEL: z.string().default('deepseek-v4-flash'),
  DB_PATH: z.string().default('./data/open-claw.db'),

  // Path to the YAML file that declares the sources.
  OPEN_CLAW_CONFIG: z.string().default('./open-claw.config.yaml'),
});

const parsed = envSchema.safeParse(process.env);

if (!parsed.success) {
  const issues = parsed.error.issues.map((i) => `  • ${i.path.join('.')}: ${i.message}`).join('\n');
  console.error(`\n✖ Invalid configuration. Fix your .env:\n${issues}\n`);
  process.exit(1);
}

const raw = parsed.data;

export const config = {
  agent: {
    provider: raw.AGENT_PROVIDER,
    workspaceRoot: path.resolve(raw.AGENT_WORKSPACE_ROOT),
    maxTurns: raw.AGENT_MAX_TURNS,
    claude: {
      apiKey: raw.ANTHROPIC_API_KEY,
      model: raw.AGENT_MODEL,
    },
    gemini: {
      apiKey: raw.GEMINI_API_KEY,
      model: raw.GEMINI_MODEL,
    },
    ollama: {
      baseUrl: raw.OLLAMA_BASE_URL.replace(/\/+$/, ''),
      model: raw.OLLAMA_MODEL,
    },
    deepseek: {
      apiKey: raw.DEEPSEEK_API_KEY,
      baseUrl: raw.DEEPSEEK_BASE_URL.replace(/\/+$/, ''),
      model: raw.DEEPSEEK_MODEL,
    },
  },
  dbPath: path.resolve(raw.DB_PATH),
  configPath: path.resolve(raw.OPEN_CLAW_CONFIG),
} as const;

if (config.agent.provider === 'gemini' && !config.agent.gemini.apiKey) {
  console.error('\n✖ AGENT_PROVIDER=gemini but GEMINI_API_KEY is empty. Set it in .env.\n');
  process.exit(1);
}

if (config.agent.provider === 'deepseek' && !config.agent.deepseek.apiKey) {
  console.error('\n✖ AGENT_PROVIDER=deepseek but DEEPSEEK_API_KEY is empty. Set it in .env.\n');
  process.exit(1);
}

/** Replace every `${VAR}` in string values with process.env[VAR]. Throws if a
 * referenced variable is unset, so misconfigured secrets fail loudly at boot. */
function expandEnvRefs(value: unknown): unknown {
  if (typeof value === 'string') {
    return value.replace(/\$\{([A-Z0-9_]+)\}/gi, (_, name: string) => {
      const v = process.env[name];
      if (v === undefined || v === '') {
        throw new Error(`config references \${${name}} but that env var is unset`);
      }
      return v;
    });
  }
  if (Array.isArray(value)) return value.map(expandEnvRefs);
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([k, v]) => [k, expandEnvRefs(v)]),
    );
  }
  return value;
}

/** Load, expand, and validate the YAML source definitions. Exits on error. */
export function loadSources(): SourceConfig[] {
  if (!fs.existsSync(config.configPath)) {
    console.error(
      `\n✖ Config file not found: ${config.configPath}\n` +
        `  Create it (see open-claw.config.example.yaml) or set OPEN_CLAW_CONFIG.\n`,
    );
    process.exit(1);
  }

  let doc: unknown;
  try {
    doc = expandEnvRefs(parseYaml(fs.readFileSync(config.configPath, 'utf8')));
  } catch (err) {
    console.error(`\n✖ Failed to load ${config.configPath}: ${(err as Error).message}\n`);
    process.exit(1);
  }

  const result = rootConfigSchema.safeParse(doc);
  if (!result.success) {
    const issues = result.error.issues
      .map((i) => `  • sources.${i.path.join('.')}: ${i.message}`)
      .join('\n');
    console.error(`\n✖ Invalid ${config.configPath}:\n${issues}\n`);
    process.exit(1);
  }

  const ids = result.data.sources.map((s) => s.id);
  const dup = ids.find((id, i) => ids.indexOf(id) !== i);
  if (dup) {
    console.error(`\n✖ Duplicate source id "${dup}" in ${config.configPath} — ids must be unique.\n`);
    process.exit(1);
  }

  return result.data.sources;
}
