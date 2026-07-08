import path from 'node:path';
import { createLogger } from '../../logger.js';
import { type GithubMeta, type TaskRow } from '../../db.js';
import type { Source, SourceDeps, GithubSourceConfig } from '../types.js';
import { isAuthenticated } from './gh.js';
import { GithubPoller } from './poller.js';
import { MergeWatcher } from './merge-watcher.js';
import { runAnswer } from './answer.js';
import { runAutofix } from './autofix.js';

const log = createLogger('github');

/** A GitHub source: polls one repo, routes labelled issues to answer/autofix,
 * and (optionally) auto-merges green PRs that pass the blast-radius policy. */
class GithubSource implements Source {
  readonly type = 'github';
  readonly id: string;
  private readonly cfg: GithubSourceConfig;
  private readonly poller: GithubPoller;
  private readonly watcher: MergeWatcher;

  constructor(cfg: GithubSourceConfig, deps: SourceDeps) {
    // Normalize the clone dir to an absolute path once, up front.
    this.cfg = { ...cfg, cloneDir: path.resolve(cfg.cloneDir) };
    this.id = cfg.id;
    this.poller = new GithubPoller(this.cfg, deps.queue);
    this.watcher = new MergeWatcher(this.cfg);
  }

  async start(): Promise<void> {
    if (!(await isAuthenticated())) {
      log.warn(`[${this.id}] gh is not authenticated — run \`gh auth login\`. Source disabled.`);
      return;
    }
    this.poller.start();
    this.watcher.start();
  }

  stop(): void {
    this.poller.stop();
    this.watcher.stop();
  }

  run(task: TaskRow): Promise<string | null> {
    if (!task.meta) throw new Error('github task has no meta');
    const meta = JSON.parse(task.meta) as GithubMeta;
    return meta.kind === 'question' ? runAnswer(this.cfg, task) : runAutofix(this.cfg, task);
  }
}

export function createGithubSource(cfg: GithubSourceConfig, deps: SourceDeps): Source {
  return new GithubSource(cfg, deps);
}
