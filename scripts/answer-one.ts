// One-off: run the real question-answer flow for a single issue, using the
// github source config from open-claw.config.yaml.
// Usage: tsx scripts/answer-one.ts <issueNumber> [sourceId]
import { loadSources } from '../src/config.js';
import { runAnswer } from '../src/sources/github/answer.js';
import type { GithubSourceConfig } from '../src/sources/types.js';
import type { GithubMeta, TaskRow } from '../src/db.js';

const issue = Number(process.argv[2]);
const wantId = process.argv[3];
if (!Number.isInteger(issue)) throw new Error('usage: answer-one.ts <issueNumber> [sourceId]');

const github = loadSources().filter((s): s is GithubSourceConfig => s.type === 'github');
const cfg = wantId ? github.find((s) => s.id === wantId) : github[0];
if (!cfg) throw new Error('no matching github source in open-claw.config.yaml');

const raw = await fetch(`https://api.github.com/repos/${cfg.repo}/issues/${issue}`, {
  headers: { 'User-Agent': 'open-claw', Accept: 'application/vnd.github+json' },
}).then((r) => r.json());

const meta: GithubMeta = {
  sourceId: cfg.id,
  repo: cfg.repo,
  issue,
  kind: 'question',
  url: raw.html_url,
  body: raw.body ?? '',
};

const task = {
  id: -1,
  source: 'github',
  prompt: raw.title,
  cwd: '',
  status: 'running',
  chat_id: null,
  meta: JSON.stringify(meta),
  result: null,
  error: null,
  created_at: '',
  updated_at: '',
} as unknown as TaskRow;

console.log(await runAnswer(cfg, task));
process.exit(0);
