import { createLogger } from '../../logger.js';
import { tasks, type GithubMeta } from '../../db.js';
import { prChecksState, prChangedFiles, mergePR, commentIssue } from './gh.js';
import { evaluateDiff } from './policy.js';
import type { GithubSourceConfig } from '../types.js';

type MergeState = 'pending' | 'merged' | 'ci_failed' | 'manual';

/**
 * Polls open autofix PRs for one repo and merges them once CI is green AND the
 * diff passes the blast-radius policy. The merge decision is made from GitHub's
 * own Checks state — never from what the agent claimed. If checks are red or the
 * policy says no, the PR is left for a human and the issue is commented.
 */
export class MergeWatcher {
  private timer: NodeJS.Timeout | null = null;
  private readonly log;

  constructor(private readonly cfg: GithubSourceConfig) {
    this.log = createLogger(`merge-watcher:${cfg.id}`);
  }

  start(): void {
    if (!this.cfg.autoMerge) {
      this.log.info('auto-merge disabled — watcher not started');
      return;
    }
    this.log.info(`auto-merge watcher every ${this.cfg.pollIntervalSec}s`);
    const tick = () => void this.tick().catch((e) => this.log.error('tick failed', e));
    this.timer = setInterval(tick, this.cfg.pollIntervalSec * 1000);
    tick();
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  private setState(taskId: number, meta: GithubMeta, state: MergeState): void {
    tasks.setMeta(taskId, { ...meta, mergeState: state });
  }

  private async tick(): Promise<void> {
    const rows = tasks.awaitingMerge();
    for (const row of rows) {
      if (!row.meta) continue;
      const meta = JSON.parse(row.meta) as GithubMeta & { mergeState?: MergeState };
      const { repo, issue, prNumber } = meta;
      if (prNumber == null) continue;
      if (repo !== this.cfg.repo) continue; // another repo's watcher owns this PR

      const state = await prChecksState(repo, prNumber);
      if (state === 'pending') continue; // check again next tick

      if (state === 'failure') {
        this.log.warn(`PR #${prNumber}: CI red — leaving for review`);
        this.setState(row.id, meta, 'ci_failed');
        await commentIssue(repo, issue, `🤖 CI is red on PR #${prNumber} — leaving it for manual review.`);
        continue;
      }

      // state is 'success' or 'none'. If there are literally no checks, do not
      // auto-merge blindly — require at least one green check.
      if (state === 'none') {
        this.log.warn(`PR #${prNumber}: no CI checks found — leaving for review`);
        this.setState(row.id, meta, 'manual');
        await commentIssue(
          repo,
          issue,
          `🤖 PR #${prNumber} has no CI checks — cannot auto-merge, leaving it for review.`,
        );
        continue;
      }

      // Green. Now the blast-radius policy on the actual diff.
      const files = await prChangedFiles(repo, prNumber);
      const verdict = evaluateDiff(files, this.cfg.policy);
      if (!verdict.ok) {
        this.log.warn(`PR #${prNumber}: policy blocked — ${verdict.reasons.join('; ')}`);
        this.setState(row.id, meta, 'manual');
        await commentIssue(
          repo,
          issue,
          `🤖 CI is green, but the auto-merge policy did not pass:\n- ${verdict.reasons.join('\n- ')}\n\nLeaving PR #${prNumber} for manual review.`,
        );
        continue;
      }

      this.log.info(`PR #${prNumber}: green + policy ok → merging`);
      await mergePR(repo, prNumber);
      this.setState(row.id, meta, 'merged');
      await commentIssue(repo, issue, `✅ open-claw merged PR #${prNumber} (CI green, policy passed).`);
    }
  }
}
