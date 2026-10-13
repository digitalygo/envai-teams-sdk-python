import test from 'node:test';
import assert from 'node:assert/strict';
import { analyzeCommits } from '../analyze-commits.mjs';
import { notification, notify } from '../notify.mjs';
import prepare from '../prepare-package.cjs';

const analyze = (...messages) => analyzeCommits({}, { cwd: process.cwd(), commits: messages.map(message => ({ message, hash: '0123456789' })), logger: { log() {} } });
for (const [message, expected] of [
  ['feat: add support', 'minor'], ['fix: handle error', 'patch'],
  ['feat!: breaking API', 'major'], ['fix(api)!: breaking API', 'major'],
  ['fix: API\n\nBREAKING CHANGE: compatibility', 'major'],
  ['chore: trigger build', 'patch'], ['refactor: simplify', 'patch'], ['build: update dependency', 'patch'],
  ['docs: guide', null], ['ci: workflow', null], ['test: coverage', null], ['style: format', null], ['perf: faster', null],
  ['feat: skip [skip ci]', null], ['ci!: pipeline only', null],
]) test(message, async () => assert.equal(await analyze(message), expected));
test('highest bump wins', async () => assert.equal(await analyze('fix: error', 'feat: API', 'chore: rebuild'), 'minor'));
test('release notifications start with exact outcome emoji', () => {
  for (const [outcome, emoji] of [['success', '✅'], ['failure', '❌']]) assert.ok(notification({ outcome, repository: 'owner/repo', tag: 'v0.2.1', commit: 'abc', runUrl: 'https://github.com' }).startsWith(emoji));
});
test('missing webhook skips without failing', async () => assert.deepEqual(await notify({}), { skipped: true }));
test('webhook never accepts HTTP or redirects', async () => {
  await assert.rejects(notify({ KCHAT_WEBHOOK_URL: 'http://example.test' }), /HTTPS/);
  await notify({ KCHAT_WEBHOOK_URL: 'https://example.test', RELEASE_OUTCOME: 'success' }, async (_, request) => {
    assert.equal(request.redirect, 'error'); assert.equal(JSON.parse(request.body).text[0], '✅'); return { ok: true };
  });
});
test('webhook errors never expose URL', async () => {
  await assert.rejects(notify({ KCHAT_WEBHOOK_URL: 'https://example.test/secret' }, async () => { throw new Error('https://example.test/secret'); }), error => !error.message.includes('secret'));
});
test('Python versions conform to PEP 440', () => {
  assert.equal(prepare.pythonVersion('1.0.0-alpha.1'), '1.0.0a1');
  assert.equal(prepare.pythonVersion('1.0.0-beta.2'), '1.0.0b2');
  assert.equal(prepare.pythonVersion('1.0.0-dev.3'), '1.0.0.dev3');
  assert.equal(prepare.pythonVersion('1.0.0'), '1.0.0');
});
