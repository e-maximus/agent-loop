import { config } from '../config.js';
import { createLogger } from '../logger.js';
import { executeTool } from './tools.js';
import { toOpenAITools, stripThinking } from './schema.js';
import type { AgentBackend, AgentResult, RunAgentOptions } from './types.js';

const log = createLogger('ollama');

/** One tool call as returned by Ollama's /api/chat. */
interface OllamaToolCall {
  function: { name: string; arguments: Record<string, unknown> };
}

/** A chat message in the Ollama wire format. */
interface OllamaMessage {
  role: 'system' | 'user' | 'assistant' | 'tool';
  content: string;
  tool_calls?: OllamaToolCall[];
  tool_name?: string;
}

interface OllamaChatResponse {
  message: OllamaMessage;
}

/**
 * Agent backend powered by a local Ollama server. Like the Gemini backend, the
 * server only provides the "brain": we own the tool-use loop and the executor,
 * so the permission gate runs on every call. Runs entirely on localhost with no
 * per-second rate limits, unlike hosted APIs.
 */
export class OllamaBackend implements AgentBackend {
  readonly name = 'ollama';
  private readonly tools = toOpenAITools();

  async run(opts: RunAgentOptions): Promise<AgentResult> {
    const messages: OllamaMessage[] = [
      { role: 'system', content: opts.systemPrompt },
      { role: 'user', content: opts.prompt },
    ];

    let finalText: string | null = null;
    let ok = false;
    let turns = 0;

    for (turns = 1; turns <= config.agent.maxTurns; turns++) {
      const message = await this.chat(messages);
      messages.push(message);

      const text = stripThinking(message.content);
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
        const args = (call.function?.arguments ?? {}) as Record<string, unknown>;
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
        messages.push({ role: 'tool', tool_name: name, content: result });
      }
    }

    if (!ok && finalText === null) {
      finalText = `⚠️ stopped after ${config.agent.maxTurns} turns without a final answer`;
    }
    log.info(`finished: ok=${ok} turns=${turns}`);
    return { text: finalText, turns, costUsd: 0, ok };
  }

  private async chat(messages: OllamaMessage[]): Promise<OllamaMessage> {
    const res = await fetch(`${config.agent.ollama.baseUrl}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: config.agent.ollama.model,
        messages,
        tools: this.tools,
        stream: false,
      }),
    });
    if (!res.ok) {
      const body = await res.text().catch(() => '');
      throw new Error(`Ollama /api/chat ${res.status}: ${body.slice(0, 500)}`);
    }
    const data = (await res.json()) as OllamaChatResponse;
    return data.message;
  }
}
