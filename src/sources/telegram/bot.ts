import { Bot, InlineKeyboard } from 'grammy';
import { randomUUID } from 'node:crypto';
import { config } from '../../config.js';
import { createLogger } from '../../logger.js';
import { tasks } from '../../db.js';
import type { Queue } from '../../queue.js';
import type { ApprovalRequest, Notifier } from '../../notify.js';
import type { TelegramSourceConfig } from '../types.js';

const log = createLogger('bot');

interface PendingApproval {
  resolve: (approved: boolean) => void;
  chatId: number;
}

/** grammY long-polling bot for one Telegram source instance. Implements the
 * Notifier interface so the task worker can report progress and ask for
 * approvals. Task intake tags each task with the owning source id. */
export class TelegramBot implements Notifier {
  readonly bot: Bot;
  private readonly pending = new Map<string, PendingApproval>();
  readonly workspaceRoot: string;

  constructor(
    private readonly cfg: TelegramSourceConfig,
    private readonly queue: Queue,
  ) {
    this.bot = new Bot(cfg.botToken);
    this.workspaceRoot = cfg.workspaceRoot ?? config.agent.workspaceRoot;
    this.installAuth();
    this.installCommands();
    this.installApprovalHandler();
    this.installTaskIntake();
  }

  // ── Notifier interface ────────────────────────────────────────────
  async send(chatId: number, text: string): Promise<void> {
    await this.bot.api.sendMessage(chatId, text, { parse_mode: 'Markdown' }).catch(async () => {
      // Fall back to plain text if Markdown fails to parse.
      await this.bot.api.sendMessage(chatId, text).catch(() => {});
    });
  }

  async requestApproval(chatId: number, req: ApprovalRequest): Promise<boolean> {
    const token = randomUUID();
    const keyboard = new InlineKeyboard()
      .text('✅ Allow', `ok:${token}`)
      .text('❌ Deny', `no:${token}`);

    await this.bot.api.sendMessage(
      chatId,
      `⚠️ Task #${req.taskId} is requesting permission:\n\n${req.summary}`,
      { parse_mode: 'Markdown', reply_markup: keyboard },
    );

    return new Promise<boolean>((resolve) => {
      this.pending.set(token, { resolve, chatId });
    });
  }

  // ── Wiring ────────────────────────────────────────────────────────
  private installAuth(): void {
    const allowed = new Set(this.cfg.allowedUserIds);
    this.bot.use(async (ctx, next) => {
      const uid = ctx.from?.id;
      if (uid == null || !allowed.has(uid)) {
        log.warn(`blocked message from unauthorized user ${uid ?? 'unknown'}`);
        if (ctx.chat) await ctx.reply('⛔ Not authorized.').catch(() => {});
        return; // do not call next() — hard stop
      }
      await next();
    });
  }

  private installCommands(): void {
    this.bot.command('start', (ctx) =>
      ctx.reply(
        "Hi! I'm open-claw. Send a task as text — I'll run it on the local machine.\n\n" +
          `Workspace: ${this.workspaceRoot}\n\n` +
          'Commands: /tasks, /status <id>, /cancel <id>',
      ),
    );
    this.bot.command('help', (ctx) =>
      ctx.reply('Send a task as text. /tasks — active tasks, /status <id>, /cancel <id>.'),
    );

    this.bot.command('tasks', (ctx) => {
      const active = tasks.listActive();
      if (active.length === 0) return ctx.reply('No active tasks.');
      const lines = active.map((t) => `#${t.id} [${t.status}] ${t.prompt.slice(0, 60)}`);
      return ctx.reply(lines.join('\n'));
    });

    this.bot.command('status', (ctx) => {
      const id = Number(ctx.match);
      if (!Number.isInteger(id)) return ctx.reply('Usage: /status <id>');
      const t = tasks.get(id);
      if (!t) return ctx.reply(`Task #${id} not found.`);
      const body = t.status === 'done' ? t.result : t.status === 'failed' ? t.error : '(in progress)';
      return ctx.reply(`#${t.id} [${t.status}]\n${(body ?? '').slice(0, 3500)}`);
    });

    this.bot.command('cancel', (ctx) => {
      const id = Number(ctx.match);
      if (!Number.isInteger(id)) return ctx.reply('Usage: /cancel <id>');
      const t = tasks.get(id);
      if (!t) return ctx.reply(`Task #${id} not found.`);
      if (t.status !== 'queued') {
        return ctx.reply(`Only a queued task can be cancelled. #${id} is currently: ${t.status}.`);
      }
      tasks.setStatus(id, 'cancelled');
      return ctx.reply(`Task #${id} cancelled.`);
    });
  }

  private installApprovalHandler(): void {
    this.bot.on('callback_query:data', async (ctx) => {
      const data = ctx.callbackQuery.data;
      const [action, token] = data.split(':');
      const entry = token ? this.pending.get(token) : undefined;
      if (!token || !entry) {
        await ctx.answerCallbackQuery({ text: 'Request expired.' });
        return;
      }
      this.pending.delete(token);
      const approved = action === 'ok';
      entry.resolve(approved);
      await ctx.answerCallbackQuery({ text: approved ? 'Allowed' : 'Denied' });
      await ctx.editMessageReplyMarkup({}).catch(() => {});
      await ctx.reply(approved ? '✅ OK, continuing.' : '❌ Denied.').catch(() => {});
    });
  }

  private installTaskIntake(): void {
    this.bot.on('message:text', async (ctx) => {
      const text = ctx.message.text.trim();
      if (text.startsWith('/')) return; // commands handled above
      const chatId = ctx.chat.id;
      const task = tasks.create('telegram', text, this.workspaceRoot, chatId, {
        sourceId: this.cfg.id,
      });
      this.queue.poke();
      await ctx.reply(`📥 Task #${task.id} accepted and queued.`);
    });
  }

  async start(): Promise<void> {
    // Report queue lifecycle back to the originating chat — but only for tasks
    // that belong to this source instance.
    const mine = (task: { meta: string | null }): boolean => {
      if (!task.meta) return false;
      try {
        return (JSON.parse(task.meta) as { sourceId?: string }).sourceId === this.cfg.id;
      } catch {
        return false;
      }
    };
    this.queue.on('done', (task, result: string | null) => {
      if (mine(task) && task.chat_id != null) {
        void this.send(task.chat_id, `✅ Task #${task.id} is done:\n\n${(result ?? '(no answer)').slice(0, 3500)}`);
      }
    });
    this.queue.on('failed', (task, error: string) => {
      if (mine(task) && task.chat_id != null) {
        void this.send(task.chat_id, `❌ Task #${task.id} failed:\n\n${String(error).slice(0, 1500)}`);
      }
    });

    // grammy long polling — works behind NAT, no public IP needed.
    void this.bot.start({
      onStart: (info) => log.info(`Telegram bot @${info.username} (${this.cfg.id}) started (long polling)`),
    });
  }

  stop(): void {
    void this.bot.stop();
  }
}
