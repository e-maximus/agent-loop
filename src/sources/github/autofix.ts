import { createLogger } from '../../logger.js';
import { runAgent, READ_ONLY_TOOLS, PATH_KEYS, type CanUseTool } from '../../agent.js';
import type { PermissionResult } from '@anthropic-ai/claude-agent-sdk';
import { tasks, type GithubMeta, type TaskRow } from '../../db.js';
import path from 'node:path';
import { describeTool } from '../../backends/tools.js';
import type { GithubSourceConfig } from '../types.js';
import {
  ensureClone,
  defaultBranch,
  prepareBranch,
  hasChanges,
  commitAll,
  push,
  openPR,
  commentIssue,
} from './gh.js';

const log = createLogger('autofix');

const BUG_PROMPT = `You are an autonomous engineering agent fixing a BUG reported in a GitHub issue, in a cloned repository. Nobody is watching — your final message is what the human reads.

This repo has its own AGENTS.md/CLAUDE.md — follow them. In particular, if it says the framework differs from your training data, READ the referenced docs before writing code.

Workflow you MUST follow:
1. Understand the issue. Explore the code (Read/Grep/Glob).
2. Install dependencies if needed (npm ci).
3. Reproduce the problem where feasible. If you genuinely cannot reproduce it, STOP and clearly say so — do not guess-fix.
4. Make a minimal, targeted change. Do not refactor unrelated code, do not touch CI config, package manifests, or lockfiles unless the issue is specifically about them.
5. Verify: run \`npm run lint\` and \`npm run build\` and make sure BOTH pass. If they fail, fix and re-run until green.
6. End with a concise summary: what was wrong, what you changed, and the lint/build result.

Write ALL output in English only — your summary, any code comments, and identifiers. Never use another language.

Do not commit, push, or open a PR yourself — the orchestrator does that.`;

const FEATURE_PROMPT = `You are an autonomous engineering agent implementing a FEATURE REQUEST from a GitHub issue, in a cloned repository. Nobody is watching — your final message is what the human reads.

This repo has its own AGENTS.md/CLAUDE.md — follow them. If it says the framework differs from your training data, READ the referenced docs before writing code.

Be conservative — a feature is riskier than a bug fix:
1. Understand exactly what is requested. If the request is vague or would require large/architectural changes, DO the smallest reasonable version and clearly note in your summary what you did and did NOT do, so a human can decide.
2. Install dependencies if needed (npm ci).
3. Implement a minimal, focused change that matches the codebase's existing patterns and design system. Do not touch CI config, package manifests, or lockfiles unless strictly required.
4. Verify: run \`npm run lint\` and \`npm run build\` — BOTH must pass. Fix and re-run until green.
5. End with a concise summary of what you implemented and the lint/build result.

Write ALL output in English only — your summary, any code comments, and identifiers. Never use another language.

Do not commit, push, or open a PR yourself — the orchestrator does that.`;

/**
 * Handles one github autofix task: clone → branch → agent fixes & verifies
 * locally (lint+build) → commit → push → open PR. Auto-merge is handled
 * separately by the merge watcher once CI is green.
 */
export async function runAutofix(cfg: GithubSourceConfig, task: TaskRow): Promise<string | null> {
  if (!task.meta) throw new Error('autofix task has no meta');
  const meta = JSON.parse(task.meta) as GithubMeta;
  const { repo, issue, kind } = meta;

  log.info(`autofix ${kind} #${issue} in ${repo}`);
  const repoPath = await ensureClone(repo, cfg.cloneDir);
  const base = await defaultBranch(repo);
  const branch = `issue-${issue}`;
  await prepareBranch(repoPath, branch);

  // In the isolated clone the agent may run freely; the only hard boundary is
  // that file paths must stay inside this checkout. The real safety gate for
  // anything reaching main is CI + the blast-radius policy before merge.
  const canUseTool: CanUseTool = async (toolName, input): Promise<PermissionResult> => {
    if (READ_ONLY_TOOLS.has(toolName)) return { behavior: 'allow', updatedInput: input };
    for (const key of PATH_KEYS) {
      const val = input[key];
      if (typeof val === 'string') {
        const abs = path.resolve(repoPath, val);
        if (abs !== repoPath && !abs.startsWith(repoPath + path.sep)) {
          return { behavior: 'deny', message: `Path ${val} escapes the repo checkout.` };
        }
      }
    }
    return { behavior: 'allow', updatedInput: input };
  };

  const verb = kind === 'feature' ? 'Implement this feature request' : 'Fix this bug';
  const prompt = `${verb} from a GitHub issue.\n\nTitle: ${task.prompt}\n\n${meta.body || '(no body)'}\n\nIssue: ${meta.url}`;

  const result = await runAgent({
    prompt,
    cwd: repoPath,
    systemPrompt: kind === 'feature' ? FEATURE_PROMPT : BUG_PROMPT,
    canUseTool,
    onToolUse: (name, input) => log.info(`#${issue} → ${describeTool(name, input)}`),
  });

  const summary = result.text ?? '(agent produced no summary)';

  if (!(await hasChanges(repoPath))) {
    log.warn(`autofix #${issue}: agent made no changes`);
    await commentIssue(
      repo,
      issue,
      `🤖 open-claw made no changes for this task.\n\n${summary.slice(0, 3000)}`,
    );
    return `No changes. ${summary}`;
  }

  const prefix = kind === 'feature' ? 'feat' : 'fix';
  const title = `${prefix}: ${task.prompt}`.slice(0, 100);
  await commitAll(repoPath, `${title}\n\nCloses #${issue}\n\n🤖 open-claw`);
  await push(repoPath, branch);

  const body = `Automated ${kind === 'feature' ? 'feature PR' : 'fix'} for issue #${issue} by open-claw.\n\nCloses #${issue}\n\n---\n${summary.slice(0, 3000)}`;
  const pr = await openPR(repoPath, { base, title, body });

  tasks.setMeta(task.id, { ...meta, branch, prNumber: pr.number, prUrl: pr.url });
  await commentIssue(
    repo,
    issue,
    `🤖 open-claw opened a PR: ${pr.url}\n\n**What changed:**\n\n${summary.slice(0, 2000)}`,
  );

  log.info(`autofix #${issue}: PR ${pr.url}`);
  const mergeNote = cfg.autoMerge
    ? 'Waiting for green CI to auto-merge.'
    : 'Auto-merge is off — PR is waiting for review.';
  return `PR opened: ${pr.url}\n${mergeNote}`;
}
