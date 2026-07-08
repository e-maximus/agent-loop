// Dry-run verification of the question follow-up logic against live GitHub
// data — no DB writes, no comments posted.
// Usage: tsx scripts/verify-question-flow.ts <issue> [sourceId]
import { loadSources } from '../src/config.js';
import { listIssueComments, isBotComment } from '../src/sources/github/gh.js';
import type { GithubSourceConfig } from '../src/sources/types.js';

const issue = Number(process.argv[2] ?? 4);
const wantId = process.argv[3];
const github = loadSources().filter((s): s is GithubSourceConfig => s.type === 'github');
const cfg = wantId ? github.find((s) => s.id === wantId) : github[0];
if (!cfg) throw new Error('no github source in open-claw.config.yaml');
const repo = cfg.repo;

const comments = await listIssueComments(repo, issue);
console.log(`#${issue} (${repo}) has ${comments.length} comment(s):`);
for (const c of comments) {
  const who = isBotComment(c.body) ? 'BOT ' : 'HUMAN';
  console.log(`  [${who}] ${c.author} @ ${c.createdAt}: ${c.body.slice(0, 60).replace(/\n/g, ' ')}…`);
}

const latestHumanAt = comments
  .filter((c) => !isBotComment(c.body))
  .reduce((max, c) => (c.createdAt > max ? c.createdAt : max), '');

const cursorAfterFirstAnswer = '';
console.log(`\nlatestHumanAt = ${latestHumanAt || '(none)'}`);
console.log(`Scenario A — cursor='' just answered, no human reply yet:`);
console.log(`  re-trigger? ${!!(latestHumanAt && latestHumanAt > cursorAfterFirstAnswer)} (expect false)`);
console.log(`Scenario B — a human comment arrives at 2099-01-01T00:00:00Z:`);
console.log(`  re-trigger? ${'2099-01-01T00:00:00Z' > cursorAfterFirstAnswer} (expect true)`);
process.exit(0);
