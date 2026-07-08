import { createLogger } from '../../logger.js';
import { runAgent, READ_ONLY_TOOLS, type CanUseTool } from '../../agent.js';
import type { PermissionResult } from '@anthropic-ai/claude-agent-sdk';
import { tasks, type GithubMeta, type TaskRow } from '../../db.js';
import { describeTool } from '../../backends/tools.js';
import type { GithubSourceConfig } from '../types.js';
import {
  ensureClone,
  commentIssue,
  listIssueComments,
  isBotComment,
  BOT_COMMENT_PREFIX,
} from './gh.js';

const log = createLogger('answer');

function systemPrompt(cfg: GithubSourceConfig): string {
  const { bug, feature } = cfg.labels;
  return `You are answering a GitHub issue that is a QUESTION about a cloned codebase. You are not fixing anything and not changing any files — just answering.

- Explore the repo (Read/Grep/Glob) to ground your answer in the actual code.
- Be concise, correct, and specific. Reference files/functions by path when useful.
- If the question is ambiguous or you cannot determine the answer from the code, say what's missing.
- The issue may already contain a back-and-forth conversation. Answer the LATEST unanswered comment from the user, using the earlier messages as context.
- Do NOT offer to implement the change yourself and do NOT ask "would you like me to do this?". You only answer questions. If the answer implies a code change (a fix or a new feature), tell the user that to get it done they should open a separate issue labelled \`${bug}\` (for a bug) or \`${feature}\` (for a feature request).
- Do not modify files. Your final message is posted verbatim as a comment on the issue.
- Write your answer in English only. Never use another language.`;
}

/** Render prior comments as a plain-text transcript for the agent's prompt. */
function renderThread(comments: { author: string; body: string }[]): string {
  if (comments.length === 0) return '';
  const lines = comments.map((c) => {
    const who = isBotComment(c.body) ? 'open-claw (you, earlier)' : `user (${c.author})`;
    return `--- ${who} ---\n${c.body}`;
  });
  return `\n\nConversation so far (oldest first):\n\n${lines.join('\n\n')}`;
}

/**
 * Handles a `question` issue: read the repo, answer, post the answer as a
 * comment. No branch, no PR. The `question` label is kept so the thread stays
 * open; the conversation cursor (`answeredThrough`) marks what we've answered.
 */
export async function runAnswer(cfg: GithubSourceConfig, task: TaskRow): Promise<string | null> {
  if (!task.meta) throw new Error('question task has no meta');
  const meta = JSON.parse(task.meta) as GithubMeta;
  const { repo, issue } = meta;

  log.info(`answering question #${issue} in ${repo}`);
  const repoPath = await ensureClone(repo, cfg.cloneDir);

  // Read-only: allow inspection tools; deny anything that would mutate files.
  const canUseTool: CanUseTool = async (toolName, input): Promise<PermissionResult> => {
    if (READ_ONLY_TOOLS.has(toolName)) return { behavior: 'allow', updatedInput: input };
    if (toolName === 'Bash') {
      // Allow investigation commands but keep them inside the checkout.
      return { behavior: 'allow', updatedInput: input };
    }
    return { behavior: 'deny', message: 'Question handler is read-only; file changes are not allowed.' };
  };

  // Pull the thread so a follow-up answer has the earlier conversation as context.
  const comments = await listIssueComments(repo, issue);
  const thread = renderThread(comments);
  const prompt = `Answer this GitHub issue (a question).\n\nTitle: ${task.prompt}\n\n${meta.body || '(no body)'}${thread}\n\nIssue: ${meta.url}`;

  const result = await runAgent({
    prompt,
    cwd: repoPath,
    systemPrompt: systemPrompt(cfg),
    canUseTool,
    onToolUse: (name, input) => log.info(`#${issue} → ${describeTool(name, input)}`),
  });

  const answer = result.text ?? '(agent produced no answer)';
  await commentIssue(repo, issue, `${BOT_COMMENT_PREFIX}\n\n${answer.slice(0, 60000)}`);

  // Advance the conversation cursor: record the newest human comment we just
  // answered, so the poller only re-triggers on comments posted after this.
  const latestHumanAt = comments
    .filter((c) => !isBotComment(c.body))
    .reduce((max, c) => (c.createdAt > max ? c.createdAt : max), '');
  tasks.setMeta(task.id, { ...meta, answeredThrough: latestHumanAt });

  return `Answered question #${issue}.`;
}
