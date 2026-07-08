import Database from 'better-sqlite3';
import fs from 'node:fs';
import path from 'node:path';
import { config } from './config.js';

export type TaskStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled';
export type TaskSource = 'telegram' | 'cron' | 'github';

export interface TaskRow {
  id: number;
  source: TaskSource;
  /** Free-form goal given to the agent. */
  prompt: string;
  /** Working directory the agent runs in (absolute). */
  cwd: string;
  status: TaskStatus;
  /** Telegram chat to report progress/results back to, if any. */
  chat_id: number | null;
  /** JSON blob with source-specific fields (e.g. github issue/PR numbers). */
  meta: string | null;
  result: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export type GithubKind = 'bug' | 'feature' | 'question';

/** Structured shape stored in TaskRow.meta for github tasks. */
export interface GithubMeta {
  /** Id of the source instance that owns this task (routes it back for run()). */
  sourceId: string;
  repo: string;
  issue: number;
  kind: GithubKind;
  url: string;
  body: string;
  branch?: string;
  prNumber?: number;
  prUrl?: string;
  // For question threads: ISO timestamp of the newest human comment we have
  // already answered. A newer human comment triggers a follow-up answer.
  answeredThrough?: string;
}

fs.mkdirSync(path.dirname(config.dbPath), { recursive: true });

export const db = new Database(config.dbPath);
db.pragma('journal_mode = WAL');
db.pragma('foreign_keys = ON');

db.exec(`
  CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,
    prompt     TEXT NOT NULL,
    cwd        TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'queued',
    chat_id    INTEGER,
    meta       TEXT,
    result     TEXT,
    error      TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
  );

  CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

  -- Snapshots for cron parsing tasks (step 6); created now so the schema is stable.
  CREATE TABLE IF NOT EXISTS snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key        TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
  );
  CREATE INDEX IF NOT EXISTS idx_snapshots_key ON snapshots(key);
`);

const stmts = {
  insert: db.prepare<[TaskSource, string, string, number | null, string | null]>(
    `INSERT INTO tasks (source, prompt, cwd, chat_id, meta) VALUES (?, ?, ?, ?, ?)`,
  ),
  byId: db.prepare<[number]>(`SELECT * FROM tasks WHERE id = ?`),
  setMeta: db.prepare<[string, number]>(
    `UPDATE tasks SET meta = ?, updated_at = datetime('now') WHERE id = ?`,
  ),
  // PRs opened by autofix that still need a merge decision.
  awaitingMerge: db.prepare(
    `SELECT * FROM tasks WHERE source = 'github'
       AND json_extract(meta, '$.prNumber') IS NOT NULL
       AND (json_extract(meta, '$.mergeState') IS NULL
            OR json_extract(meta, '$.mergeState') = 'pending')`,
  ),
  // Dedup: has this github issue already been taken (still open work or done)?
  githubIssueActive: db.prepare<[string, number]>(
    `SELECT id FROM tasks WHERE source = 'github'
       AND status IN ('queued','running','done')
       AND json_extract(meta, '$.repo') = ? AND json_extract(meta, '$.issue') = ?
     LIMIT 1`,
  ),
  // Is there work in flight for this issue right now? (queued or running)
  githubIssueInFlight: db.prepare<[string, number]>(
    `SELECT id FROM tasks WHERE source = 'github'
       AND status IN ('queued','running')
       AND json_extract(meta, '$.repo') = ? AND json_extract(meta, '$.issue') = ?
     LIMIT 1`,
  ),
  // Most recent task for an issue, any status — used to read the answer cursor.
  githubIssueLatest: db.prepare<[string, number]>(
    `SELECT * FROM tasks WHERE source = 'github'
       AND json_extract(meta, '$.repo') = ? AND json_extract(meta, '$.issue') = ?
     ORDER BY id DESC LIMIT 1`,
  ),
  claimNext: db.prepare(
    `SELECT * FROM tasks WHERE status = 'queued' ORDER BY id ASC LIMIT 1`,
  ),
  setStatus: db.prepare<[TaskStatus, number]>(
    `UPDATE tasks SET status = ?, updated_at = datetime('now') WHERE id = ?`,
  ),
  finishDone: db.prepare<[string | null, number]>(
    `UPDATE tasks SET status = 'done', result = ?, updated_at = datetime('now') WHERE id = ?`,
  ),
  finishFailed: db.prepare<[string, number]>(
    `UPDATE tasks SET status = 'failed', error = ?, updated_at = datetime('now') WHERE id = ?`,
  ),
  listActive: db.prepare(
    `SELECT * FROM tasks WHERE status IN ('queued','running') ORDER BY id ASC`,
  ),
  // On startup, any task left 'running' means the process died mid-flight.
  resetOrphans: db.prepare(
    `UPDATE tasks SET status = 'failed', error = 'interrupted (process restart)', updated_at = datetime('now') WHERE status = 'running'`,
  ),
};

export const tasks = {
  create(
    source: TaskSource,
    prompt: string,
    cwd: string,
    chatId: number | null,
    meta?: unknown,
  ): TaskRow {
    const info = stmts.insert.run(source, prompt, cwd, chatId, meta ? JSON.stringify(meta) : null);
    return stmts.byId.get(Number(info.lastInsertRowid)) as TaskRow;
  },
  get(id: number): TaskRow | undefined {
    return stmts.byId.get(id) as TaskRow | undefined;
  },
  setMeta(id: number, meta: unknown): void {
    stmts.setMeta.run(JSON.stringify(meta), id);
  },
  isGithubIssueTaken(repo: string, issue: number): boolean {
    return stmts.githubIssueActive.get(repo, issue) !== undefined;
  },
  hasGithubIssueInFlight(repo: string, issue: number): boolean {
    return stmts.githubIssueInFlight.get(repo, issue) !== undefined;
  },
  latestGithubIssueTask(repo: string, issue: number): TaskRow | undefined {
    return stmts.githubIssueLatest.get(repo, issue) as TaskRow | undefined;
  },
  awaitingMerge(): TaskRow[] {
    return stmts.awaitingMerge.all() as TaskRow[];
  },
  claimNext(): TaskRow | undefined {
    const row = stmts.claimNext.get() as TaskRow | undefined;
    if (!row) return undefined;
    stmts.setStatus.run('running', row.id);
    return { ...row, status: 'running' };
  },
  finishDone(id: number, result: string | null) {
    stmts.finishDone.run(result, id);
  },
  finishFailed(id: number, error: string) {
    stmts.finishFailed.run(error, id);
  },
  setStatus(id: number, status: TaskStatus) {
    stmts.setStatus.run(status, id);
  },
  listActive(): TaskRow[] {
    return stmts.listActive.all() as TaskRow[];
  },
  resetOrphans(): number {
    return stmts.resetOrphans.run().changes;
  },
};
