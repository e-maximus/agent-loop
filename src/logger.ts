import fs from 'node:fs';
import path from 'node:path';
import { currentTaskLog } from './task-log.js';

type Level = 'debug' | 'info' | 'warn' | 'error';

const order: Record<Level, number> = { debug: 0, info: 1, warn: 2, error: 3 };
const min: Level = (process.env.LOG_LEVEL as Level) ?? 'info';

// Lifecycle logs (startup, poller, bot) that don't belong to a task go here.
// Per-task work is routed to its own file via task-log's AsyncLocalStorage.
const LOG_DIR = path.resolve('./data/logs');
const LOG_FILE = path.join(LOG_DIR, 'open-claw.log');

let generalStream: fs.WriteStream | null = null;
function generalFile(): fs.WriteStream {
  if (!generalStream) {
    fs.mkdirSync(LOG_DIR, { recursive: true });
    generalStream = fs.createWriteStream(LOG_FILE, { flags: 'a' });
  }
  return generalStream;
}

function log(level: Level, scope: string, msg: string, extra?: unknown) {
  const ts = new Date().toISOString();
  const line = `${ts} ${level.toUpperCase().padEnd(5)} [${scope}] ${msg}`;

  // Inside a task: write to that task's file. Otherwise: the general log.
  // Files get debug-and-up always; console honors LOG_LEVEL.
  const extraStr = extra !== undefined ? ' ' + safeStringify(extra) : '';
  try {
    (currentTaskLog()?.stream ?? generalFile()).write(line + extraStr + '\n');
  } catch {
    /* never let logging crash the app */
  }

  if (order[level] < order[min]) return;
  const fn = level === 'error' ? console.error : level === 'warn' ? console.warn : console.log;
  if (extra !== undefined) fn(line, extra);
  else fn(line);
}

function safeStringify(v: unknown): string {
  if (typeof v === 'string') return v;
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

export function createLogger(scope: string) {
  return {
    debug: (msg: string, extra?: unknown) => log('debug', scope, msg, extra),
    info: (msg: string, extra?: unknown) => log('info', scope, msg, extra),
    warn: (msg: string, extra?: unknown) => log('warn', scope, msg, extra),
    error: (msg: string, extra?: unknown) => log('error', scope, msg, extra),
  };
}

export const logFilePath = LOG_FILE;
