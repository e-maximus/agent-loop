import { createLogger } from '../../logger.js';
import { tasks, type GithubKind } from '../../db.js';
import { listOpenIssues, listIssueComments, isBotComment, type Issue } from './gh.js';
import type { Queue } from '../../queue.js';
import type { GithubSourceConfig } from '../types.js';

/**
 * Polls one configured repo for open issues and enqueues a task for each new
 * one that carries a routing label. Bug/feature issues are one-shot; question
 * issues form a continuing thread (dedup by an `answeredThrough` cursor).
 */
export class GithubPoller {
  private timer: NodeJS.Timeout | null = null;
  private readonly log;

  constructor(
    private readonly cfg: GithubSourceConfig,
    private readonly queue: Queue,
  ) {
    this.log = createLogger(`gh-poller:${cfg.id}`);
  }

  /** Map an issue's labels to a handler kind. Priority: bug > feature > question. */
  private classify(issue: Issue): GithubKind | null {
    const labels = new Set(issue.labels);
    const { bug, feature, question } = this.cfg.labels;
    if (labels.has(bug)) return 'bug';
    if (labels.has(feature)) return 'feature';
    if (labels.has(question)) return 'question';
    return null;
  }

  start(): void {
    const { bug, feature, question } = this.cfg.labels;
    this.log.info(
      `polling ${this.cfg.repo} every ${this.cfg.pollIntervalSec}s ` +
        `(labels: ${bug}→fix, ${feature}→feature, ${question}→answer)`,
    );
    const tick = () => void this.tick().catch((e) => this.log.error('poll failed', e));
    this.timer = setInterval(tick, this.cfg.pollIntervalSec * 1000);
    tick();
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  private async tick(): Promise<void> {
    const repo = this.cfg.repo;
    const issues = await listOpenIssues(repo);
    let enqueued = 0;
    for (const issue of issues) {
      const kind = this.classify(issue);
      if (!kind) continue;
      const didEnqueue =
        kind === 'question'
          ? await this.considerQuestion(repo, issue)
          : this.considerFixable(repo, issue, kind);
      if (didEnqueue) enqueued++;
    }
    if (enqueued > 0) this.queue.poke();
  }

  /** Bug/feature issues are one-shot: enqueue once, never again. */
  private considerFixable(repo: string, issue: Issue, kind: GithubKind): boolean {
    if (tasks.isGithubIssueTaken(repo, issue.number)) return false;
    this.enqueue(repo, issue, kind);
    return true;
  }

  /**
   * Question issues are a continuing thread: answer the initial question, then
   * answer again whenever a human posts a new comment after our last reply.
   * Dedup is by a per-issue cursor (`answeredThrough`) instead of "taken once".
   */
  private async considerQuestion(repo: string, issue: Issue): Promise<boolean> {
    // Never run two answers for the same issue at once.
    if (tasks.hasGithubIssueInFlight(repo, issue.number)) return false;

    const prev = tasks.latestGithubIssueTask(repo, issue.number);
    const answeredThrough = prev?.meta
      ? (JSON.parse(prev.meta) as { answeredThrough?: string }).answeredThrough ?? ''
      : '';

    // Never answered before → answer the initial question.
    if (!prev) {
      this.enqueue(repo, issue, 'question');
      return true;
    }

    // Answered before → only re-answer if a human commented past our cursor.
    const comments = await listIssueComments(repo, issue.number);
    const latestHumanAt = comments
      .filter((c) => !isBotComment(c.body))
      .reduce((max, c) => (c.createdAt > max ? c.createdAt : max), '');
    if (latestHumanAt && latestHumanAt > answeredThrough) {
      this.enqueue(repo, issue, 'question');
      return true;
    }
    return false;
  }

  private enqueue(repo: string, issue: Issue, kind: GithubKind): void {
    tasks.create('github', issue.title, this.cfg.cloneDir, null, {
      sourceId: this.cfg.id,
      repo,
      issue: issue.number,
      kind,
      url: issue.url,
      body: issue.body,
    });
    this.log.info(`enqueued ${kind} for #${issue.number}: ${issue.title}`);
  }
}
