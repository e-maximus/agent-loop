import fs from 'node:fs';
import path from 'node:path';
import { run } from '../../exec.js';
import { createLogger } from '../../logger.js';

const log = createLogger('github');

export interface Issue {
  number: number;
  title: string;
  body: string;
  url: string;
  labels: string[];
}

export interface ChangedFile {
  file: string;
  added: number;
  deleted: number;
}

export interface IssueComment {
  author: string;
  body: string;
  createdAt: string; // ISO 8601
}

/** Prefix open-claw prepends to every comment it posts. Used to tell our own
 * replies apart from human comments (the bot and the repo owner may share a
 * GitHub login, so author is not a reliable signal). */
export const BOT_COMMENT_PREFIX = '🤖 **open-claw:**';

/** True if a comment body was posted by open-claw (not a human). */
export function isBotComment(body: string): boolean {
  return body.trimStart().startsWith('🤖');
}

export type ChecksState = 'pending' | 'success' | 'failure' | 'none';

/** `owner/name` → safe directory name. */
function repoDirName(repo: string): string {
  return repo.replace('/', '__');
}

export async function isAuthenticated(): Promise<boolean> {
  const res = await run('gh', ['auth', 'status'], { throwOnError: false });
  return res.code === 0;
}

/** Default branch of the repo (e.g. "main"). */
export async function defaultBranch(repo: string): Promise<string> {
  const res = await run('gh', [
    'repo', 'view', repo, '--json', 'defaultBranchRef', '--jq', '.defaultBranchRef.name',
  ]);
  return res.stdout.trim() || 'main';
}

/** All open issues (with their labels) so the caller can route by label. */
export async function listOpenIssues(repo: string): Promise<Issue[]> {
  const res = await run('gh', [
    'issue', 'list',
    '--repo', repo,
    '--state', 'open',
    '--json', 'number,title,body,url,labels',
    '--limit', '100',
  ]);
  const parsed = JSON.parse(res.stdout || '[]') as Array<{
    number: number; title: string; body: string | null; url: string;
    labels: Array<{ name: string }>;
  }>;
  return parsed.map((i) => ({
    number: i.number,
    title: i.title,
    body: i.body ?? '',
    url: i.url,
    labels: (i.labels ?? []).map((l) => l.name),
  }));
}

/** All comments on an issue, oldest first. Empty on error. */
export async function listIssueComments(repo: string, issue: number): Promise<IssueComment[]> {
  const res = await run('gh', [
    'api', `repos/${repo}/issues/${issue}/comments`, '--paginate',
  ], { throwOnError: false });
  if (res.code !== 0) return [];
  const parsed = JSON.parse(res.stdout || '[]') as Array<{
    user?: { login?: string }; body?: string; created_at?: string;
  }>;
  return parsed.map((c) => ({
    author: c.user?.login ?? 'unknown',
    body: c.body ?? '',
    createdAt: c.created_at ?? '',
  }));
}

/**
 * Clone the repo if missing, otherwise fetch and hard-reset to the latest
 * default branch. Returns the local checkout path. Idempotent.
 */
export async function ensureClone(repo: string, cloneDir: string): Promise<string> {
  fs.mkdirSync(cloneDir, { recursive: true });
  const dest = path.join(cloneDir, repoDirName(repo));
  const base = await defaultBranch(repo);

  if (!fs.existsSync(path.join(dest, '.git'))) {
    log.info(`cloning ${repo} → ${dest}`);
    await run('gh', ['repo', 'clone', repo, dest]);
  }
  // Make sure git can push using the gh credential helper.
  await run('gh', ['auth', 'setup-git'], { throwOnError: false });
  await run('git', ['fetch', 'origin', base], { cwd: dest });
  await run('git', ['checkout', base], { cwd: dest });
  await run('git', ['reset', '--hard', `origin/${base}`], { cwd: dest });
  await run('git', ['clean', '-fd'], { cwd: dest });
  return dest;
}

/** Create (or reset) a fresh working branch off the default branch. */
export async function prepareBranch(repoPath: string, branch: string): Promise<void> {
  await run('git', ['checkout', '-B', branch], { cwd: repoPath });
}

export async function hasChanges(repoPath: string): Promise<boolean> {
  const res = await run('git', ['status', '--porcelain'], { cwd: repoPath });
  return res.stdout.trim().length > 0;
}

/** Files changed on the branch vs base, with per-file line counts. */
export async function changedFiles(repoPath: string, base: string): Promise<ChangedFile[]> {
  const res = await run('git', ['diff', '--numstat', `origin/${base}...HEAD`], { cwd: repoPath });
  return res.stdout
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
    .map((line) => {
      const [added, deleted, file] = line.split('\t');
      return {
        file: file ?? '',
        added: added === '-' ? 0 : Number(added),
        deleted: deleted === '-' ? 0 : Number(deleted),
      };
    });
}

export async function commitAll(repoPath: string, message: string): Promise<void> {
  await run('git', ['add', '-A'], { cwd: repoPath });
  await run('git', ['commit', '-m', message], { cwd: repoPath });
}

export async function push(repoPath: string, branch: string): Promise<void> {
  await run('git', ['push', '-u', 'origin', branch, '--force-with-lease'], { cwd: repoPath });
}

/** Open a PR from the current branch; returns its number and url. */
export async function openPR(
  repoPath: string,
  opts: { base: string; title: string; body: string },
): Promise<{ number: number; url: string }> {
  const res = await run('gh', [
    'pr', 'create',
    '--base', opts.base,
    '--title', opts.title,
    '--body', opts.body,
  ], { cwd: repoPath });
  const url = res.stdout.trim().split('\n').pop() ?? '';
  const number = Number(url.split('/').pop());
  return { number, url };
}

/** Roll up the PR's CI checks into a single state. */
export async function prChecksState(repo: string, prNumber: number): Promise<ChecksState> {
  const res = await run('gh', [
    'pr', 'view', String(prNumber),
    '--repo', repo,
    '--json', 'statusCheckRollup',
  ], { throwOnError: false });
  if (res.code !== 0) return 'pending';

  const data = JSON.parse(res.stdout || '{}') as {
    statusCheckRollup?: Array<{ status?: string; conclusion?: string; state?: string }>;
  };
  const checks = data.statusCheckRollup ?? [];
  if (checks.length === 0) return 'none';

  let anyPending = false;
  for (const c of checks) {
    // CheckRun uses status/conclusion; StatusContext uses state.
    const status = c.status; // QUEUED | IN_PROGRESS | COMPLETED
    const conclusion = (c.conclusion ?? c.state ?? '').toUpperCase();
    if (status && status !== 'COMPLETED') anyPending = true;
    else if (['FAILURE', 'ERROR', 'CANCELLED', 'TIMED_OUT', 'ACTION_REQUIRED'].includes(conclusion)) {
      return 'failure';
    } else if (conclusion === 'PENDING' || conclusion === 'EXPECTED') {
      anyPending = true;
    }
  }
  return anyPending ? 'pending' : 'success';
}

/** Files changed by a PR, via the GitHub API (no local checkout needed). */
export async function prChangedFiles(repo: string, prNumber: number): Promise<ChangedFile[]> {
  const res = await run('gh', [
    'pr', 'view', String(prNumber),
    '--repo', repo,
    '--json', 'files',
  ]);
  const data = JSON.parse(res.stdout || '{}') as {
    files?: Array<{ path: string; additions: number; deletions: number }>;
  };
  return (data.files ?? []).map((f) => ({
    file: f.path,
    added: f.additions ?? 0,
    deleted: f.deletions ?? 0,
  }));
}

export async function mergePR(repo: string, prNumber: number): Promise<void> {
  await run('gh', [
    'pr', 'merge', String(prNumber),
    '--repo', repo,
    '--squash',
    '--delete-branch',
  ]);
}

export async function commentIssue(repo: string, issue: number, body: string): Promise<void> {
  await run('gh', ['issue', 'comment', String(issue), '--repo', repo, '--body', body], {
    throwOnError: false,
  });
}

export async function removeLabel(repo: string, issue: number, label: string): Promise<void> {
  await run('gh', [
    'issue', 'edit', String(issue), '--repo', repo, '--remove-label', label,
  ], { throwOnError: false });
}
