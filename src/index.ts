import { config, loadSources } from './config.js';
import { createLogger } from './logger.js';
import { tasks } from './db.js';
import { Queue } from './queue.js';
import { buildSources } from './sources/registry.js';
import type { Source } from './sources/types.js';

const log = createLogger('main');

function activeModel(): string {
  switch (config.agent.provider) {
    case 'claude':
      return config.agent.claude.model;
    case 'gemini':
      return config.agent.gemini.model;
    case 'ollama':
      return config.agent.ollama.model;
    case 'deepseek':
      return config.agent.deepseek.model;
  }
}

async function main() {
  log.info('open-claw starting…');
  log.info(`workspace root: ${config.agent.workspaceRoot}`);
  log.info(`provider: ${config.agent.provider}, model: ${activeModel()}, max turns: ${config.agent.maxTurns}`);

  const orphans = tasks.resetOrphans();
  if (orphans > 0) log.warn(`marked ${orphans} interrupted task(s) as failed`);

  const sourceConfigs = loadSources();

  // The queue dispatches each task to the source instance that created it,
  // identified by meta.sourceId.
  let sources: Map<string, Source>;
  const queue = new Queue((task) => {
    const sourceId = task.meta
      ? (JSON.parse(task.meta) as { sourceId?: string }).sourceId
      : undefined;
    const src = sourceId ? sources.get(sourceId) : undefined;
    if (!src) {
      return Promise.reject(
        new Error(`task #${task.id}: no source for sourceId=${sourceId ?? '(none)'}`),
      );
    }
    return src.run(task);
  }, 1);

  sources = buildSources(sourceConfigs, { queue });

  queue.start();
  for (const s of sources.values()) await s.start();
  log.info(
    `started ${sources.size} source(s): ${[...sources.values()].map((s) => `${s.id}(${s.type})`).join(', ')}`,
  );

  queue.poke(); // pick up anything already queued (e.g. before a restart)

  const shutdown = () => {
    log.info('shutting down…');
    queue.stop();
    for (const s of sources.values()) s.stop();
    process.exit(0);
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

main().catch((err) => {
  log.error('fatal', err instanceof Error ? err.stack : err);
  process.exit(1);
});
