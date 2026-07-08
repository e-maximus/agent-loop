import { execFile } from 'node:child_process';

export interface RunResult {
  stdout: string;
  stderr: string;
  code: number;
}

export interface RunOptions {
  cwd?: string;
  /** Extra environment variables merged over process.env. */
  env?: Record<string, string>;
  /** Reject the promise on a non-zero exit code (default true). */
  throwOnError?: boolean;
  timeoutMs?: number;
}

/**
 * Thin promise wrapper over execFile. Uses argv arrays (no shell) so task
 * input can never be interpreted as shell — important since prompts/issue
 * text flow into git/gh arguments.
 */
export function run(cmd: string, args: string[], opts: RunOptions = {}): Promise<RunResult> {
  const { cwd, env, throwOnError = true, timeoutMs = 10 * 60_000 } = opts;
  return new Promise((resolve, reject) => {
    execFile(
      cmd,
      args,
      { cwd, env: { ...process.env, ...env }, timeout: timeoutMs, maxBuffer: 64 * 1024 * 1024 },
      (error, stdout, stderr) => {
        const code = error && typeof error.code === 'number' ? error.code : error ? 1 : 0;
        const result: RunResult = { stdout: stdout.toString(), stderr: stderr.toString(), code };
        if (error && throwOnError) {
          reject(
            new Error(
              `\`${cmd} ${args.join(' ')}\` failed (exit ${code}):\n${result.stderr || result.stdout}`,
            ),
          );
        } else {
          resolve(result);
        }
      },
    );
  });
}
