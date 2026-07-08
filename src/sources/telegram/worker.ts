import path from 'node:path';
import { runAgent, READ_ONLY_TOOLS, PATH_KEYS, type CanUseTool } from '../../agent.js';
import type { PermissionResult } from '@anthropic-ai/claude-agent-sdk';
import type { Notifier } from '../../notify.js';
import type { TaskRow } from '../../db.js';

function isInsideWorkspace(p: string, root: string): boolean {
  const abs = path.resolve(root, p);
  return abs === root || abs.startsWith(root + path.sep);
}

function summarizeTool(toolName: string, input: Record<string, unknown>): string {
  switch (toolName) {
    case 'Bash':
      return `🖥️ run command:\n\`${String(input.command ?? '').slice(0, 500)}\``;
    case 'Write':
      return `📝 create/overwrite file: \`${input.file_path}\``;
    case 'Edit':
      return `✏️ edit file: \`${input.file_path}\``;
    default:
      return `🔧 ${toolName} (${Object.keys(input).join(', ') || 'no args'})`;
  }
}

function summarizeToolUse(name: string, input: Record<string, unknown>): string {
  switch (name) {
    case 'Bash':
      return `running: \`${String(input.command ?? '').slice(0, 120)}\``;
    case 'Read':
      return `reading \`${input.file_path}\``;
    case 'Edit':
    case 'Write':
      return `editing \`${input.file_path}\``;
    case 'Grep':
      return `searching \`${input.pattern}\``;
    default:
      return `${name}`;
  }
}

const SYSTEM_PROMPT = `You are an autonomous engineering agent running on the user's local machine, driven remotely via Telegram. The user is not watching a terminal — communicate outcomes clearly in your final answer.

Rules:
- Work only inside the allowed workspace. Never touch files outside it.
- When fixing a bug: reproduce it first (a failing test or repro script). If you cannot reproduce it, stop and report that instead of guessing.
- After a change, run the project's tests/build locally and report the result.
- Prefer minimal, targeted changes. Do not refactor unrelated code.
- Sensitive actions (shell commands, writing files, installing packages) are gated for human approval — keep them small and explain them.
- End with a concise summary of what you did and the outcome.
- Write ALL output in English only — your summary, any code comments, and identifiers. Never use another language.`;

/**
 * Telegram-driven task runner. Sensitive tools are gated via approval buttons
 * in Telegram; file operations outside the given workspace root are auto-denied.
 */
export function makeTaskRunner(notifier: Notifier, workspaceRoot: string) {
  return async function runTask(task: TaskRow): Promise<string | null> {
    const chatId = task.chat_id;
    const notify = async (text: string) => {
      if (chatId != null) await notifier.send(chatId, text).catch(() => {});
    };

    await notify(`▶️ Picked up task #${task.id}. Working directory: \`${task.cwd}\``);

    const canUseTool: CanUseTool = async (toolName, input): Promise<PermissionResult> => {
      if (READ_ONLY_TOOLS.has(toolName)) return { behavior: 'allow', updatedInput: input };

      for (const key of PATH_KEYS) {
        const val = input[key];
        if (typeof val === 'string' && !isInsideWorkspace(val, workspaceRoot)) {
          await notify(`⛔ Auto-denied: path is outside the workspace — \`${val}\``);
          return {
            behavior: 'deny',
            message: `Path ${val} is outside the allowed workspace root ${workspaceRoot}.`,
          };
        }
      }

      if (chatId == null) {
        return { behavior: 'deny', message: 'No interactive approver available for this task.' };
      }

      const approved = await notifier.requestApproval(chatId, {
        taskId: task.id,
        summary: summarizeTool(toolName, input),
      });
      return approved
        ? { behavior: 'allow', updatedInput: input }
        : { behavior: 'deny', message: 'User denied this action in Telegram.' };
    };

    const result = await runAgent({
      prompt: task.prompt,
      cwd: task.cwd,
      systemPrompt: SYSTEM_PROMPT,
      canUseTool,
      onToolUse: (name, input) => void notify(`… ${summarizeToolUse(name, input)}`),
    });

    return result.text;
  };
}
