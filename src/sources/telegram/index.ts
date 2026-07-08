import type { TaskRow } from '../../db.js';
import type { Source, SourceDeps, TelegramSourceConfig } from '../types.js';
import { TelegramBot } from './bot.js';
import { makeTaskRunner } from './worker.js';

/** A Telegram source: a long-polling bot that accepts free-form tasks and runs
 * them in a workspace, gating sensitive actions via inline approval buttons. */
class TelegramSource implements Source {
  readonly type = 'telegram';
  readonly id: string;
  private readonly bot: TelegramBot;
  private readonly run_: (task: TaskRow) => Promise<string | null>;

  constructor(cfg: TelegramSourceConfig, deps: SourceDeps) {
    this.id = cfg.id;
    this.bot = new TelegramBot(cfg, deps.queue);
    this.run_ = makeTaskRunner(this.bot, this.bot.workspaceRoot);
  }

  start(): Promise<void> {
    return this.bot.start();
  }

  stop(): void {
    this.bot.stop();
  }

  run(task: TaskRow): Promise<string | null> {
    return this.run_(task);
  }
}

export function createTelegramSource(cfg: TelegramSourceConfig, deps: SourceDeps): Source {
  return new TelegramSource(cfg, deps);
}
