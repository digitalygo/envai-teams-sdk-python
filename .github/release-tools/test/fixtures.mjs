import { execFile } from 'node:child_process';
import { mkdtemp, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { promisify } from 'node:util';

export const execute = promisify(execFile);

export function fixtureEnvironment(home) {
  return {
    PATH: process.env.PATH,
    HOME: home,
    LANG: 'C.UTF-8',
    CI: 'false',
    GIT_CONFIG_NOSYSTEM: '1',
    GIT_CONFIG_GLOBAL: '/dev/null',
    GIT_ALLOW_PROTOCOL: 'file',
    GIT_TERMINAL_PROMPT: '0',
    GIT_AUTHOR_NAME: 'SDK Runtime Test',
    GIT_AUTHOR_EMAIL: 'runtime-test@example.invalid',
    GIT_COMMITTER_NAME: 'SDK Runtime Test',
    GIT_COMMITTER_EMAIL: 'runtime-test@example.invalid',
  };
}

export async function createGitFixture(branch = 'main') {
  const root = await mkdtemp(join(tmpdir(), 'envai-sdk-release-'));
  const cwd = join(root, 'work');
  const remote = join(root, 'remote.git');
  const env = fixtureEnvironment(root);
  await mkdir(cwd);

  const git = async (...args) => (await execute('git', args, { cwd, env })).stdout.trim();
  await git('init', '--initial-branch=main');
  await git('init', '--bare', '--initial-branch=main', remote);
  await git('commit', '--allow-empty', '-m', 'feat: initial fixture');
  await git('tag', 'v1.0.0');

  for (const name of ['alpha', 'beta', 'dev', '1.x', '2.3.x', 'feature/envai']) {
    await git('branch', name);
  }

  await git('checkout', branch);
  await git('commit', '--allow-empty', '-m', 'chore: validate release dependencies');
  await git('remote', 'add', 'origin', remote);
  await git('push', '--all', 'origin');
  await git('push', 'origin', 'v1.0.0');

  return { root, cwd, remote, env, git };
}
