import type { ChangedFile } from './gh.js';

export interface PolicyVerdict {
  ok: boolean;
  reasons: string[];
}

/** Blast-radius policy for auto-merge, per github source instance. */
export interface MergePolicy {
  allowedGlobs: string[];
  maxChangedLines: number;
}

/** Minimal glob matcher supporting `**` and `*` — enough for path prefixes. */
function globToRegExp(glob: string): RegExp {
  const escaped = glob
    .replace(/[.+^${}()|[\]\\]/g, '\\$&')
    .replace(/\*\*/g, ' ') // placeholder for **
    .replace(/\*/g, '[^/]*')
    .replace(/ /g, '.*');
  return new RegExp(`^${escaped}$`);
}

/**
 * Decide whether a diff is safe to auto-merge:
 *  - every changed file must match an allowed glob (blast radius), and
 *  - total changed lines must be within the configured limit.
 * A failing verdict means "leave the PR for human review", not "reject".
 */
export function evaluateDiff(files: ChangedFile[], policy: MergePolicy): PolicyVerdict {
  const reasons: string[] = [];
  const allowMatchers = policy.allowedGlobs.map(globToRegExp);
  const isAllowedPath = (file: string): boolean => allowMatchers.some((re) => re.test(file));

  if (files.length === 0) {
    return { ok: false, reasons: ['no changed files'] };
  }

  const outside = files.map((f) => f.file).filter((f) => !isAllowedPath(f));
  if (outside.length > 0) {
    reasons.push(`files outside allowed globs: ${outside.join(', ')}`);
  }

  const totalLines = files.reduce((sum, f) => sum + f.added + f.deleted, 0);
  if (totalLines > policy.maxChangedLines) {
    reasons.push(`diff too large: ${totalLines} lines > limit ${policy.maxChangedLines}`);
  }

  return { ok: reasons.length === 0, reasons };
}
