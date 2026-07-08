// Verifies config-driven source loading + instantiation (no ingress started).
// Usage: tsx scripts/verify-sources.ts
import { loadSources } from '../src/config.js';
import { buildSources } from '../src/sources/registry.js';
import { Queue } from '../src/queue.js';

const configs = loadSources();
console.log(`loaded ${configs.length} source config(s):`);
for (const c of configs) {
  const detail = c.type === 'github' ? `repo=${c.repo} autoMerge=${c.autoMerge}` : `users=${c.allowedUserIds.length}`;
  console.log(`  - ${c.id} (${c.type}) ${detail}`);
}

const queue = new Queue(() => Promise.resolve(null), 1);
const sources = buildSources(configs, { queue });
console.log(`\ninstantiated ${sources.size} source(s): ${[...sources.values()].map((s) => `${s.id}(${s.type})`).join(', ')}`);
process.exit(0);
