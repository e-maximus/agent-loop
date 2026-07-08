import { AsyncLocalStorage } from 'node:async_hooks';
import fs from 'node:fs';
import path from 'node:path';
import type { TaskRow } from './db.js';

const TASK_LOG_DIR = path.resolve('./data/logs/tasks');

interface TaskLogCtx {
  stream: fs.WriteStream;
  file: string;
}

// Any code running inside runWithTaskLog() can discover the active task's log
// sink via the logger, without the task id being threaded through every call.
const store = new AsyncLocalStorage<TaskLogCtx>();

export function currentTaskLog(): TaskLogCtx | undefined {
  return store.getStore();
}

function stamp(d = new Date()): string {
  const p = (n: number) => String(n).padStart(2, '0');
  return (
    `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}` +
    `-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`
  );
}

/** Short identifier for the file name: issue<N> for github, task<id> otherwise. */
function keyFor(task: TaskRow): string {
  if (task.source === 'github' && task.meta) {
    try {
      const m = JSON.parse(task.meta) as { issue?: number };
      if (m.issue) return `issue${m.issue}`;
    } catch {
      /* fall through */
    }
  }
  return `task${task.id}`;
}

export function taskLogPath(task: TaskRow): string {
  return path.join(TASK_LOG_DIR, `${task.source}-${keyFor(task)}-${stamp()}.log`);
}

/** Run `fn` with a dedicated per-task log file active. Returns the file path
 *  via the callback so the caller can surface it. */
export async function runWithTaskLog<T>(
  task: TaskRow,
  fn: (logFile: string) => Promise<T>,
): Promise<T> {
  fs.mkdirSync(TASK_LOG_DIR, { recursive: true });
  const file = taskLogPath(task);
  const stream = fs.createWriteStream(file, { flags: 'a' });
  try {
    return await store.run({ stream, file }, () => fn(file));
  } finally {
    // Wait for the file to actually flush/close before returning, so callers
    // that exit immediately afterwards (e.g. the run-issue script) don't drop
    // buffered log lines.
    await new Promise<void>((resolve) => stream.end(resolve));
  }
}
