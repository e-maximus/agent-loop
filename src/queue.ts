import { EventEmitter } from 'node:events';
import { tasks, type TaskRow } from './db.js';
import { createLogger } from './logger.js';
import { runWithTaskLog } from './task-log.js';

const log = createLogger('queue');

export type TaskRunner = (task: TaskRow) => Promise<string | null>;

/**
 * In-process queue backed by SQLite. Single machine, so no Redis needed:
 * tasks are persisted in the `tasks` table and this class just pumps them
 * through a bounded worker pool. Survives restarts (queued tasks are picked
 * up again; interrupted 'running' ones are marked failed on boot in db.ts).
 */
export class Queue extends EventEmitter {
  private running = 0;
  private stopped = false;

  constructor(
    private readonly runner: TaskRunner,
    private readonly concurrency = 1,
  ) {
    super();
  }

  /** Wake the pump — call after enqueuing a task. */
  poke(): void {
    if (this.stopped) return;
    queueMicrotask(() => this.pump());
  }

  start(): void {
    this.stopped = false;
    this.pump();
  }

  stop(): void {
    this.stopped = true;
  }

  private pump(): void {
    if (this.stopped) return;
    while (this.running < this.concurrency) {
      const task = tasks.claimNext();
      if (!task) break;
      this.running++;
      this.execute(task).finally(() => {
        this.running--;
        this.poke();
      });
    }
  }

  private async execute(task: TaskRow): Promise<void> {
    this.emit('start', task);
    try {
      const result = await runWithTaskLog(task, async (logFile) => {
        log.info(`▶ task #${task.id} (${task.source}) started — log: ${logFile}`);
        return this.runner(task);
      });
      tasks.finishDone(task.id, result);
      log.info(`✓ task #${task.id} done`);
      this.emit('done', task, result);
    } catch (err) {
      const message = err instanceof Error ? (err.stack ?? err.message) : String(err);
      tasks.finishFailed(task.id, message);
      log.error(`✗ task #${task.id} failed`, message);
      this.emit('failed', task, message);
    }
  }
}
