import { GoogleGenAI, type Part } from '@google/genai';
import { config } from '../config.js';
import { createLogger } from '../logger.js';
import { toolDeclarations, executeTool } from './tools.js';
import type { AgentBackend, AgentResult, RunAgentOptions } from './types.js';

const log = createLogger('gemini');

/**
 * Agent backend powered by the Gemini API. Unlike the Claude SDK, the API only
 * provides the "brain": we own the tool-use loop and the tool executor, so the
 * permission gate runs on every single call exactly as with Claude.
 */
export class GeminiBackend implements AgentBackend {
  readonly name = 'gemini';
  private readonly ai = new GoogleGenAI({ apiKey: config.agent.gemini.apiKey });

  async run(opts: RunAgentOptions): Promise<AgentResult> {
    const chat = this.ai.chats.create({
      model: config.agent.gemini.model,
      config: {
        systemInstruction: opts.systemPrompt,
        tools: [{ functionDeclarations: toolDeclarations }],
      },
    });

    let message: Part[] = [{ text: opts.prompt }];
    let finalText: string | null = null;
    let ok = false;
    let turns = 0;

    for (turns = 1; turns <= config.agent.maxTurns; turns++) {
      const response = await chat.sendMessage({ message });
      const calls = response.functionCalls ?? [];
      const text = response.text;
      if (text && text.trim()) opts.onText?.(text);

      if (calls.length === 0) {
        finalText = text ?? null;
        ok = true;
        break;
      }

      // Execute each requested tool through the permission gate, then feed the
      // results back so the model can continue.
      const results: Part[] = [];
      for (const call of calls) {
        const name = call.name ?? '';
        const args = (call.args ?? {}) as Record<string, unknown>;
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
        results.push({ functionResponse: { id: call.id, name, response: { result } } });
      }
      message = results;
    }

    if (!ok && finalText === null) {
      finalText = `⚠️ stopped after ${config.agent.maxTurns} turns without a final answer`;
    }
    log.info(`finished: ok=${ok} turns=${turns}`);
    return { text: finalText, turns, costUsd: undefined, ok };
  }
}
