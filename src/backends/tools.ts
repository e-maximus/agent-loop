import fs from 'node:fs';
import path from 'node:path';
import { Type, type FunctionDeclaration } from '@google/genai';
import { run } from '../exec.js';

const READ_MAX_CHARS = 30_000;
const RESULT_MAX_CHARS = 20_000;
const GREP_MAX_MATCHES = 100;
const GLOB_MAX = 200;

/** Function declarations advertised to the model. Names/params mirror the
 *  Claude tool set so the permission gate (READ_ONLY_TOOLS, PATH_KEYS) and the
 *  Telegram summaries work identically across backends. */
export const toolDeclarations: FunctionDeclaration[] = [
  {
    name: 'Read',
    description: 'Read a file from the filesystem. Returns its contents with line numbers.',
    parameters: {
      type: Type.OBJECT,
      properties: {
        file_path: { type: Type.STRING, description: 'Path to the file (absolute or relative to cwd).' },
        offset: { type: Type.NUMBER, description: 'Optional 1-based start line.' },
        limit: { type: Type.NUMBER, description: 'Optional number of lines to read.' },
      },
      required: ['file_path'],
    },
  },
  {
    name: 'Write',
    description: 'Create or overwrite a file with the given content.',
    parameters: {
      type: Type.OBJECT,
      properties: {
        file_path: { type: Type.STRING },
        content: { type: Type.STRING },
      },
      required: ['file_path', 'content'],
    },
  },
  {
    name: 'Edit',
    description:
      'Replace an exact string in a file. old_string must be unique unless replace_all is true.',
    parameters: {
      type: Type.OBJECT,
      properties: {
        file_path: { type: Type.STRING },
        old_string: { type: Type.STRING },
        new_string: { type: Type.STRING },
        replace_all: { type: Type.BOOLEAN },
      },
      required: ['file_path', 'old_string', 'new_string'],
    },
  },
  {
    name: 'Bash',
    description: 'Run a shell command in the working directory and return its output.',
    parameters: {
      type: Type.OBJECT,
      properties: {
        command: { type: Type.STRING },
      },
      required: ['command'],
    },
  },
  {
    name: 'Grep',
    description: 'Search file contents for a regular expression. Returns matching file:line entries.',
    parameters: {
      type: Type.OBJECT,
      properties: {
        pattern: { type: Type.STRING },
        path: { type: Type.STRING, description: 'Optional subdir to scope the search.' },
      },
      required: ['pattern'],
    },
  },
  {
    name: 'Glob',
    description: 'List files matching a glob pattern (e.g. "src/**/*.ts").',
    parameters: {
      type: Type.OBJECT,
      properties: {
        pattern: { type: Type.STRING },
      },
      required: ['pattern'],
    },
  },
];

/** Compact one-line description of a tool call, for progress logs. */
export function describeTool(name: string, input: Record<string, unknown>): string {
  switch (name) {
    case 'Bash':
      return `Bash: ${String(input.command ?? '').slice(0, 160)}`;
    case 'Read':
      return `Read ${input.file_path}`;
    case 'Write':
      return `Write ${input.file_path}`;
    case 'Edit':
      return `Edit ${input.file_path}`;
    case 'Grep':
      return `Grep ${input.pattern}`;
    case 'Glob':
      return `Glob ${input.pattern}`;
    default:
      return `${name}(${Object.keys(input).join(', ')})`;
  }
}

function truncate(s: string, max = RESULT_MAX_CHARS): string {
  return s.length > max ? s.slice(0, max) + `\n… [truncated ${s.length - max} chars]` : s;
}

function resolveIn(cwd: string, p: string): string {
  return path.isAbsolute(p) ? p : path.resolve(cwd, p);
}

function listFiles(cwd: string): string[] {
  // fs.globSync (Node 22+) with node_modules/.git excluded.
  return fs
    .globSync('**/*', {
      cwd,
      exclude: (p) => p.includes('node_modules') || p.includes('.git/') || p.startsWith('.git'),
    })
    .filter((rel) => {
      try {
        return fs.statSync(path.join(cwd, rel)).isFile();
      } catch {
        return false;
      }
    });
}

/** Execute one tool call. Never throws — returns an error string the model can
 *  read and adapt to. */
export async function executeTool(
  name: string,
  args: Record<string, unknown>,
  cwd: string,
): Promise<string> {
  try {
    switch (name) {
      case 'Read': {
        const file = resolveIn(cwd, String(args.file_path));
        const raw = fs.readFileSync(file, 'utf8');
        const lines = raw.split('\n');
        const offset = typeof args.offset === 'number' ? Math.max(1, args.offset) : 1;
        const limit = typeof args.limit === 'number' ? args.limit : lines.length;
        const slice = lines.slice(offset - 1, offset - 1 + limit);
        const numbered = slice.map((l, i) => `${offset + i}\t${l}`).join('\n');
        return truncate(numbered, READ_MAX_CHARS);
      }
      case 'Write': {
        const file = resolveIn(cwd, String(args.file_path));
        fs.mkdirSync(path.dirname(file), { recursive: true });
        fs.writeFileSync(file, String(args.content ?? ''));
        return `Wrote ${file}`;
      }
      case 'Edit': {
        const file = resolveIn(cwd, String(args.file_path));
        const oldStr = String(args.old_string ?? '');
        const newStr = String(args.new_string ?? '');
        const content = fs.readFileSync(file, 'utf8');
        const count = content.split(oldStr).length - 1;
        if (count === 0) return `Error: old_string not found in ${file}`;
        if (count > 1 && args.replace_all !== true) {
          return `Error: old_string is not unique in ${file} (${count} matches). Pass replace_all or add context.`;
        }
        const updated =
          args.replace_all === true ? content.split(oldStr).join(newStr) : content.replace(oldStr, newStr);
        fs.writeFileSync(file, updated);
        return `Edited ${file} (${count} replacement${count > 1 ? 's' : ''})`;
      }
      case 'Bash': {
        const res = await run('bash', ['-lc', String(args.command ?? '')], {
          cwd,
          throwOnError: false,
        });
        const out = [
          `exit code: ${res.code}`,
          res.stdout && `stdout:\n${res.stdout}`,
          res.stderr && `stderr:\n${res.stderr}`,
        ]
          .filter(Boolean)
          .join('\n');
        return truncate(out);
      }
      case 'Grep': {
        const pattern = new RegExp(String(args.pattern), 'i');
        const base = args.path ? resolveIn(cwd, String(args.path)) : cwd;
        const files = listFiles(base);
        const matches: string[] = [];
        for (const rel of files) {
          if (matches.length >= GREP_MAX_MATCHES) break;
          const abs = path.join(base, rel);
          let text: string;
          try {
            text = fs.readFileSync(abs, 'utf8');
          } catch {
            continue;
          }
          const fileLines = text.split('\n');
          for (let i = 0; i < fileLines.length; i++) {
            if (pattern.test(fileLines[i]!)) {
              matches.push(`${rel}:${i + 1}: ${fileLines[i]!.trim().slice(0, 200)}`);
              if (matches.length >= GREP_MAX_MATCHES) break;
            }
          }
        }
        return matches.length ? truncate(matches.join('\n')) : 'No matches.';
      }
      case 'Glob': {
        const hits = fs
          .globSync(String(args.pattern), {
            cwd,
            exclude: (p) => p.includes('node_modules') || p.includes('.git'),
          })
          .slice(0, GLOB_MAX);
        return hits.length ? hits.join('\n') : 'No files matched.';
      }
      default:
        return `Error: unknown tool "${name}"`;
    }
  } catch (err) {
    return `Error running ${name}: ${err instanceof Error ? err.message : String(err)}`;
  }
}
