import type { Source, SourceConfig, SourceDeps, SourceFactory } from './types.js';
import { createTelegramSource } from './telegram/index.js';
import { createGithubSource } from './github/index.js';

/** Maps a source `type` to the factory that builds an instance of it. Add new
 * source types here. */
const factories: { [T in SourceConfig['type']]: SourceFactory<Extract<SourceConfig, { type: T }>> } = {
  telegram: createTelegramSource,
  github: createGithubSource,
};

/** Instantiate every configured source, keyed by its unique `id`. */
export function buildSources(configs: SourceConfig[], deps: SourceDeps): Map<string, Source> {
  const map = new Map<string, Source>();
  for (const cfg of configs) {
    const factory = factories[cfg.type] as SourceFactory<SourceConfig>;
    map.set(cfg.id, factory(cfg, deps));
  }
  return map;
}
