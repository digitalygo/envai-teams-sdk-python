import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFile, realpath, rm } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { analyzeCommits } from '@semantic-release/commit-analyzer';
import expandBranches from 'semantic-release/lib/branches/expand.js';
import { createGitFixture } from './fixtures.mjs';

const runtime = new URL('../', import.meta.url);
const readJson = async (path) => JSON.parse(await readFile(new URL(path, runtime), 'utf8'));
const releaseConfig = createRequire(import.meta.url)('../../../release.config.cjs');
const { parserOpts, releaseRules } = await import('../analyze-commits.mjs');
const analyzerOptions = { parserOpts, releaseRules };

test('limits the adapter to the two reviewed release-tool consumers', async () => {
  const manifest = await readJson('package.json');
  const lock = await readJson('package-lock.json');
  const consumers = Object.entries(lock.packages)
    .filter(([path, metadata]) => path && metadata.dependencies?.micromatch)
    .map(([path, metadata]) => [path, metadata.version]);

  assert.deepEqual(consumers, [
    ['node_modules/@semantic-release/commit-analyzer', '13.0.1'],
    ['node_modules/semantic-release', '25.0.2'],
  ]);
  assert.deepEqual(manifest.overrides, {
    '@semantic-release/npm': '$@semantic-release/npm',
    'semantic-release@25.0.2': { micromatch: '$micromatch' },
    '@semantic-release/commit-analyzer@13.0.1': { micromatch: '$micromatch' },
  });
  assert.equal(manifest.dependencies.micromatch, 'file:./adapters/release-matcher');
  assert.equal(lock.packages['node_modules/micromatch'].resolved, 'adapters/release-matcher');
  assert.equal(lock.packages['node_modules/micromatch'].link, true);
  const adapterPath = await realpath(new URL('adapters/release-matcher/index.js', runtime));

  for (const [path] of consumers) {
    const require = createRequire(new URL(`${path}/package.json`, runtime));
    assert.equal(await realpath(require.resolve('micromatch')), adapterPath);
  }

  for (const [path, metadata] of Object.entries(lock.packages)) {
    assert.doesNotMatch(path, /(?:^|\/)node_modules\/braces$/);

    for (const dependencies of [metadata.dependencies, metadata.optionalDependencies, metadata.peerDependencies]) {
      assert.equal(dependencies?.braces, undefined, `${path} reintroduced braces`);
    }
  }
});

test('requires review whenever either installed matching call site changes', async () => {
  const fingerprints = {
    'node_modules/semantic-release/lib/branches/expand.js': '13c28f5e7247bf8457f62463289f8435c4dd4b1eaacf8cd8b5027b6c88cc5003',
    'node_modules/@semantic-release/commit-analyzer/lib/analyze-commit.js': 'e40acc103dc0431b003fdbc78d9a9f76cf2e31b87fcacbb8ea4ee3fdeac794f6',
  };

  for (const [path, expected] of Object.entries(fingerprints)) {
    const source = await readFile(new URL(path, runtime));
    assert.equal(createHash('sha256').update(source).digest('hex'), expected, `Review the matcher API in ${path}`);
  }
});

test('expands the actual release branches through the installed semantic-release consumer', async (context) => {
  const fixture = await createGitFixture();
  context.after(() => rm(fixture.root, { recursive: true, force: true }));
  const branches = releaseConfig.branches.map((branch) => typeof branch === 'string' ? { name: branch } : branch);

  assert.deepEqual(await expandBranches(fixture.remote, { cwd: fixture.cwd }, branches), branches);
  const maintenance = await expandBranches(fixture.remote, { cwd: fixture.cwd }, [
    { name: '+([0-9])?(.{+([0-9]),x}).x' },
  ]);
  assert.deepEqual(maintenance.map(({ name }) => name), ['1.x', '2.3.x']);
  assert.deepEqual(await expandBranches(fixture.remote, { cwd: fixture.cwd }, [{ name: 'missing-*' }]), []);
});

const commitCases = [
  ['chore: refresh locked dependencies', 'patch'],
  ['fix: correct transaction rounding', 'patch'],
  ['feat: add recurring transactions', 'minor'],
  ['feat: change payload\n\nBREAKING CHANGE: remove legacy field', 'major'],
  ['docs: clarify setup', null],
];

for (const [message, expected] of commitCases) {
  test(`preserves configured commit analysis for ${message.split('\n')[0]}`, async () => {
    assert.equal(await analyzeCommits(analyzerOptions, {
      cwd: fileURLToPath(runtime),
      commits: [{ hash: '1234567890abcdef', message }],
      logger: { log() {} },
    }), expected);
  });
}

test('preserves custom glob rules and release priority in the installed analyzer', async () => {
  const context = {
    cwd: fileURLToPath(runtime),
    commits: [
      { hash: 'a'.repeat(40), message: 'refactor(core-api): simplify requests' },
      { hash: 'b'.repeat(40), message: 'fix(ui-button): correct label' },
    ],
    logger: { log() {} },
  };
  const releaseRules = [
    { type: 'refactor', scope: 'core-*', release: 'minor' },
    { type: '{fix,perf}', scope: 'ui-*', release: 'patch' },
  ];

  assert.equal(await analyzeCommits({ releaseRules }, context), 'minor');
  assert.equal(await analyzeCommits({ releaseRules: [{ type: '*', release: false }] }, context), null);
});
