import { mkdtempSync, writeFileSync, mkdirSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
import test from 'node:test';
import assert from 'node:assert/strict';
import prepare from '../prepare-package.cjs';

for (const [version, modulePath, valid] of [
  ['1.0.0-alpha.1', 'github.com/digitalygo/envai-teams-sdk-go', true],
  ['0.2.0', 'github.com/digitalygo/envai-teams-sdk-go', true],
  ['2.0.0', 'github.com/digitalygo/envai-teams-sdk-go', false],
  ['2.0.0-beta.1', 'github.com/digitalygo/envai-teams-sdk-go/v2', true],
  ['3.0.0', 'github.com/digitalygo/envai-teams-sdk-go/v2', false],
  ['1.0.0', 'github.com/digitalygo/envai-teams-sdk-go/v2', false],
]) test(`Go module major guard: ${version} ${modulePath}`, () => {
  const cwd = mkdtempSync(join(tmpdir(), 'sdk-go-release-'));
  try {
    writeFileSync(join(cwd, 'go.mod'), `module ${modulePath}\n`);
    const run = () => prepare.verifyRelease({ language: 'go' }, { cwd, nextRelease: { version } });
    if (valid) assert.doesNotThrow(run); else assert.throws(run, /major-version suffix/);
  } finally { rmSync(cwd, { recursive: true, force: true }); }
});

test('metadata commits only selected files and pushes without rewriting history', () => {
  const root = mkdtempSync(join(tmpdir(), 'sdk-release-git-'));
  const cwd = join(root, 'work'); const remote = join(root, 'remote');
  mkdirSync(cwd);
  const git = (args, directory = cwd) => execFileSync('git', args, { cwd: directory, encoding: 'utf8', stdio: 'pipe' }).trim();
  try {
    git(['init', '--bare', remote], root); git(['init', '-b', 'alpha']);
    git(['config', 'user.name', 'Test']); git(['config', 'user.email', 'test@example.test']);
    writeFileSync(join(cwd, 'package.json'), '{"version":"0.1.0"}\n');
    writeFileSync(join(cwd, 'unrelated'), 'original');
    git(['add', '.']); git(['commit', '-m', 'feat: initial']); git(['push', remote, 'alpha']);
    const initial = git(['rev-parse', 'HEAD']);
    writeFileSync(join(cwd, 'package.json'), '{"version":"1.0.0-alpha.1"}\n');
    writeFileSync(join(cwd, 'unrelated'), 'keep local change');
    const context = { cwd, branch: { name: 'alpha' }, options: { repositoryUrl: remote }, nextRelease: { version: '1.0.0-alpha.1' } };
    prepare.commitVersion(context, ['package.json']);
    assert.equal(git(['rev-parse', 'HEAD^']), initial);
    assert.equal(git(['show', 'HEAD:unrelated']), 'original');
    assert.equal(readFileSync(join(cwd, 'unrelated'), 'utf8'), 'keep local change');
    assert.match(git(['log', '-1', '--format=%s']), /\[skip ci\]/);
    assert.equal(git(['rev-parse', 'HEAD']), git(['rev-parse', 'refs/heads/alpha'], remote));
    const releaseCommit = git(['rev-parse', 'HEAD']);
    prepare.commitVersion(context, ['package.json']);
    assert.equal(git(['rev-parse', 'HEAD']), releaseCommit);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('package build processes do not inherit credential material', () => {
  const sanitized = prepare.buildEnv({ PATH: '/bin', GITHUB_TOKEN: 'private', GH_TOKEN: 'private', KCHAT_WEBHOOK_URL: 'private', ACTIONS_ID_TOKEN_REQUEST_TOKEN: 'private', GIT_CONFIG_VALUE_0: 'private' });
  assert.deepEqual(sanitized, { PATH: '/bin' });
});

test('Python metadata replacement fails closed before writing on missing or duplicate assignments', () => {
  const cwd = mkdtempSync(join(tmpdir(), 'sdk-python-version-'));
  try {
    mkdirSync(join(cwd, 'src/envai'), { recursive: true });
    const metadata = join(cwd, 'pyproject.toml'); const module = join(cwd, 'src/envai/__init__.py');
    writeFileSync(metadata, 'version = "0.1.0"\n'); writeFileSync(module, '__version__="0.1.0"\n');
    assert.throws(() => prepare.setPythonVersion(cwd, '1.0.0a1'), /exactly one/);
    assert.equal(readFileSync(metadata, 'utf8'), 'version = "0.1.0"\n');
    writeFileSync(module, '__version__ = "0.1.0"\n__version__ = "0.2.0"\n');
    assert.throws(() => prepare.setPythonVersion(cwd, '1.0.0a1'), /exactly one/);
    writeFileSync(module, '__version__ = "0.1.0"\n');
    prepare.setPythonVersion(cwd, '1.0.0a1');
    assert.equal(readFileSync(metadata, 'utf8'), 'version = "1.0.0a1"\n');
    assert.equal(readFileSync(module, 'utf8'), '__version__ = "1.0.0a1"\n');
  } finally { rmSync(cwd, { recursive: true, force: true }); }
});
