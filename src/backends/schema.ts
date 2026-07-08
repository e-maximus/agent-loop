import type { Schema } from '@google/genai';
import { toolDeclarations } from './tools.js';

/**
 * OpenAI-style tool schema, converted from the Gemini FunctionDeclarations so
 * every backend advertises an identical tool set. Both Ollama (`/api/chat`) and
 * the DeepSeek/OpenAI (`/chat/completions`) wire formats accept this shape.
 */
export function toOpenAITools() {
  return toolDeclarations.map((d) => ({
    type: 'function' as const,
    function: {
      name: d.name,
      description: d.description,
      parameters: toJsonSchema(d.parameters),
    },
  }));
}

/** Convert a Gemini Schema (UPPERCASE Type enum) to plain JSON Schema. */
export function toJsonSchema(schema: Schema | undefined): Record<string, unknown> {
  if (!schema) return { type: 'object', properties: {} };
  const out: Record<string, unknown> = {};
  if (schema.type) out.type = String(schema.type).toLowerCase();
  if (schema.description) out.description = schema.description;
  if (schema.properties) {
    out.properties = Object.fromEntries(
      Object.entries(schema.properties).map(([k, v]) => [k, toJsonSchema(v)]),
    );
  }
  if (schema.required) out.required = schema.required;
  if (schema.items) out.items = toJsonSchema(schema.items);
  return out;
}

/** Strip reasoning models' <think>…</think> chain-of-thought from a reply. */
export function stripThinking(content: string): string {
  return (content ?? '').replace(/<think>[\s\S]*?<\/think>/g, '').trim();
}
