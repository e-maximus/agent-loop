import { query, type SDKMessage } from '@anthropic-ai/claude-agent-sdk';
import { config } from '../config.js';
import { createLogger } from '../logger.js';
import type { AgentBackend, AgentResult, RunAgentOptions } from './types.js';

const log = createLogger('claude');

/** Agent backend powered by the Claude Agent SDK (tools + loop built in). */
export class ClaudeBackend implements AgentBackend {
  readonly name = 'claude';

  async run(opts: RunAgentOptions): Promise<AgentResult> {
    let text: string | null = null;
    let turns: number | undefined;
    let costUsd: number | undefined;
    let ok = false;

    const response = query({
      prompt: opts.prompt,
      options: {
        model: config.agent.claude.model,
        cwd: opts.cwd,
        maxTurns: config.agent.maxTurns,
        permissionMode: 'default',
        systemPrompt: opts.systemPrompt,
        canUseTool: opts.canUseTool,
      },
    });

    for await (const message of response as AsyncIterable<SDKMessage>) {
      if (message.type === 'assistant') {
        for (const block of message.message.content) {
          if (block.type === 'text' && block.text.trim()) {
            opts.onText?.(block.text);
          } else if (block.type === 'tool_use') {
            opts.onToolUse?.(block.name, block.input as Record<string, unknown>);
          }
        }
      } else if (message.type === 'result') {
        ok = message.subtype === 'success';
        text = message.subtype === 'success' ? message.result : `⚠️ ${message.subtype}`;
        costUsd = 'total_cost_usd' in message ? message.total_cost_usd : undefined;
        turns = 'num_turns' in message ? message.num_turns : undefined;
        log.info(`finished: ok=${ok} turns=${turns} cost=$${costUsd?.toFixed?.(4) ?? '?'}`);
      }
    }

    return { text, turns, costUsd, ok };
  }
}
