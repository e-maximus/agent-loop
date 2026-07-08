import { config } from '../config.js';
import { createLogger } from '../logger.js';
import { executeTool } from './tools.js';
import { toOpenAITools, stripThinking } from './schema.js';
import type { AgentBackend, AgentResult, RunAgentOptions } from './types.js';

const log = createLogger('deepseek');

/** One tool call in the OpenAI/DeepSeek wire format (arguments is a JSON string). */
interface ToolCall {
  id: string;
  type: 'function';
  function: { name: string; arguments: string };
}

/** A chat message in the OpenAI/DeepSeek wire format. */
interface ChatMessage {
  role: 'system' | 'user' | 'assistant' | 'tool';
  content: string | null;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
}

interface ChatResponse {
  choices?: { message: ChatMessage }[];
  error?: { message?: string };
}

/**
 * Agent backend powered by the cloud DeepSeek API (OpenAI-compatible). Like the
 * Ollama backend, the API only provides the "brain": we own the tool-use loop
 * and the executor, so the permission gate runs on every call.
 */
export class DeepseekBackend implements AgentBackend {
  readonly name = 'deepseek';
  private readonly tools = toOpenAITools();

  async run(opts: RunAgentOptions): Promise<AgentResult> {
    const messages: ChatMessage[] = [
      { role: 'system', content: opts.systemPrompt },
      { role: 'user', content: opts.prompt },
    ];

    let finalText: string | null = null;
    let ok = false;
    let turns = 0;

    for (turns = 1; turns <= config.agent.maxTurns; turns++) {
      const message = await this.chat(messages);
      messages.push(message);

      const text = stripThinking(message.content ?? '');
      const calls = message.tool_calls ?? [];
      if (text) opts.onText?.(text);

      if (calls.length === 0) {
        finalText = text || null;
        ok = true;
        break;
      }

      // Execute each requested tool through the permission gate, then feed the
      // results back so the model can continue.
      for (const call of calls) {
        const name = call.function?.name ?? '';
        const args = parseArgs(call.function?.arguments);
        opts.onToolUse?.(name, args);

        const permission = await opts.canUseTool(name, args);
        let result: string;
        if (permission.behavior === 'deny') {
          result = `Permission denied: ${permission.message}`;
        } else {
          const input =
            permission.behavior === 'allow'
              ? ((permission.updatedInput as Record<string, unknown>) ?? args)
              : args;
          result = await executeTool(name, input, opts.cwd);
        }
        messages.push({ role: 'tool', tool_call_id: call.id, content: result });
      }
    }

    if (!ok && finalText === null) {
      finalText = `⚠️ stopped after ${config.agent.maxTurns} turns without a final answer`;
    }
    log.info(`finished: ok=${ok} turns=${turns}`);
    return { text: finalText, turns, costUsd: undefined, ok };
  }

  private async chat(messages: ChatMessage[]): Promise<ChatMessage> {
    const res = await fetch(`${config.agent.deepseek.baseUrl}/chat/completions`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${config.agent.deepseek.apiKey}`,
      },
      body: JSON.stringify({
        model: config.agent.deepseek.model,
        messages,
        tools: this.tools,
        stream: false,
      }),
    });
    const body = (await res.json().catch(() => ({}))) as ChatResponse;
    const choice = body.choices?.[0];
    if (!res.ok || !choice) {
      const detail = body.error?.message ?? JSON.stringify(body).slice(0, 500);
      throw new Error(`DeepSeek /chat/completions ${res.status}: ${detail}`);
    }
    return choice.message;
  }
}

/** Tool-call arguments arrive as a JSON string; tolerate malformed output. */
function parseArgs(raw: string | undefined): Record<string, unknown> {
  if (!raw) return {};
  try {
    return JSON.parse(raw) as Record<string, unknown>;
  } catch {
    return {};
  }
}
