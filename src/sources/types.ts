import { z } from 'zod';
import type { TaskRow } from '../db.js';
import type { Queue } from '../queue.js';

/**
 * A source is a self-contained integration (Telegram, GitHub, …). It owns its
 * ingress (starts a bot / polls an API and enqueues tasks) and knows how to
 * execute a task that originated from it. Multiple instances of the same type
 * can run at once (e.g. several GitHub repos), each identified by `id`.
 */
export interface Source {
  readonly id: string;
  readonly type: string;
  /** Begin ingress: start the bot / start polling. */
  start(): Promise<void> | void;
  /** Stop ingress and release timers/connections. */
  stop(): void;
  /** Execute a task that belongs to this source instance. */
  run(task: TaskRow): Promise<string | null>;
}

/** Shared dependencies handed to every source factory. */
export interface SourceDeps {
  queue: Queue;
}

export type SourceFactory<C = unknown> = (cfg: C, deps: SourceDeps) => Source;

// ── Per-type config schemas (validated after YAML parse + ${VAR} expansion) ──

const telegramSourceSchema = z.object({
  type: z.literal('telegram'),
  id: z.string().min(1),
  botToken: z.string().min(1),
  allowedUserIds: z.array(z.coerce.number().int()).nonempty(),
  // Working directory tasks run in; defaults to agent.workspaceRoot when unset.
  workspaceRoot: z.string().optional(),
});

const githubSourceSchema = z.object({
  type: z.literal('github'),
  id: z.string().min(1),
  repo: z.string().min(1), // owner/name
  labels: z
    .object({
      bug: z.string().default('bug'),
      feature: z.string().default('enhancement'),
      question: z.string().default('question'),
    })
    .default({}),
  pollIntervalSec: z.number().int().positive().default(120),
  autoMerge: z.boolean().default(false),
  cloneDir: z.string().default('./data/repos'),
  policy: z
    .object({
      allowedGlobs: z.array(z.string()).default(['src/**', 'public/**', 'docs/**']),
      maxChangedLines: z.number().int().positive().default(300),
    })
    .default({}),
});

export const sourceConfigSchema = z.discriminatedUnion('type', [
  telegramSourceSchema,
  githubSourceSchema,
]);

export const rootConfigSchema = z.object({
  sources: z.array(sourceConfigSchema).min(1, 'config must define at least one source'),
});

export type TelegramSourceConfig = z.infer<typeof telegramSourceSchema>;
export type GithubSourceConfig = z.infer<typeof githubSourceSchema>;
export type SourceConfig = z.infer<typeof sourceConfigSchema>;
