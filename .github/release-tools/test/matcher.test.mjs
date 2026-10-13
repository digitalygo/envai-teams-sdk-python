import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import match from '../adapters/release-matcher/index.js';
import { execute, fixtureEnvironment } from './fixtures.mjs';

let oracle;
let oracleDirectory;

before(async () => {
  oracleDirectory = await mkdtemp(join(tmpdir(), 'envai-sdk-micromatch-oracle-'));
  await writeFile(join(oracleDirectory, 'package.json'), '{"name":"release-matching-test-oracle","private":true}');
  const env = {
    ...fixtureEnvironment(oracleDirectory),
    npm_config_cache: join(oracleDirectory, '.npm-cache'),
    npm_config_userconfig: '/dev/null',
    npm_config_registry: 'https://registry.npmjs.org',
  };

  for (const key of ['HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy', 'NODE_EXTRA_CA_CERTS']) {
    if (process.env[key]) {
      env[key] = process.env[key];
    }
  }

  await execute('npm', [
    'install', '--prefix', oracleDirectory, '--ignore-scripts', '--no-audit', '--no-fund',
    '--package-lock=false', '--save-exact', 'micromatch@4.0.8', 'picomatch@2.3.2',
  ], { cwd: oracleDirectory, env, timeout: 120000 });
  const require = createRequire(join(oracleDirectory, 'package.json'));
  assert.equal(require('micromatch/package.json').version, '4.0.8');
  assert.equal(require('picomatch/package.json').version, '2.3.2');
  oracle = require('micromatch');
});

after(async () => {
  if (oracleDirectory) {
    await rm(oracleDirectory, { recursive: true, force: true });
  }
});

const candidates = [
  'main', 'beta', 'alpha', 'feature/envai', '1.x', '2.3.x', 'next', 'next-major',
  '.hidden', 'release/v1', 'release/v2', 'release/v10', 'fix/a', 'fix/b', 'fix/a',
  'chore', 'feat', 'fix', 'constructor', 'toString', 'release/{literal}',
];

const patterns = [
  'main', '{main,beta,alpha}', '*', '!*', '!beta', '!(beta)', '+(alpha|beta)',
  '+([0-9])?(.{+([0-9]),x}).x', 'release/v{1,2}', 'release/v[1-9]',
  'feature/**', '@(fix|feat)', '[[:alpha:]]*', '*!(beta)', '**', '**/a',
  [], ['*', '!beta'], ['!beta', 'beta'], ['alpha', 'beta', '!alpha', 'alpha'],
  ['!beta', '!alpha'], ['beta', 'alpha'], ['main', 'main'], ['*', '!beta', 'beta'],
  'constructor', 'toString', 'release/\\{literal\\}',
];

for (const pattern of patterns) {
  test(`matches micromatch ordering and negation for ${JSON.stringify(pattern)}`, () => {
    for (const list of [candidates, [], ['alpha', 'alpha', 'beta'], 'alpha']) {
      assert.deepEqual(match(list, pattern), oracle(list, pattern));
    }
  });
}

test('matches micromatch isMatch across commit-rule patterns', () => {
  const rulePatterns = patterns.filter((pattern) => !Array.isArray(pattern) || pattern.length > 0);

  for (const value of [...candidates, '', 'core-api', 'ui-button', 'dependency update']) {
    for (const pattern of rulePatterns) {
      assert.equal(match.isMatch(value, pattern), oracle.isMatch(value, pattern), `${value}: ${JSON.stringify(pattern)}`);
    }
  }
});

test('rejects options until every consumer is reviewed', () => {
  for (const options of [{}, { nocase: true }, { nobrace: true }, null, false]) {
    assert.throws(() => match(['main'], 'main', options), /options are unsupported/);
    assert.throws(() => match.isMatch('main', 'main', options), /options are unsupported/);
  }
});

test('rejects unsupported APIs instead of silently changing their behavior', () => {
  for (const property of ['braces', 'braceExpand', 'parse', 'makeRe', 'matcher', 'scan', 'not', 'match', 'unknown']) {
    assert.throws(() => match[property], /Unsupported release matching API/);
  }

  assert.equal(match.name, 'match');
  assert.equal(match[Symbol.toStringTag], undefined);
});

test('handles adversarial nested patterns without invoking brace expansion', { timeout: 5000 }, () => {
  const nested = '{'.repeat(4500) + 'value' + '}'.repeat(4500);
  assert.deepEqual(match(['main', 'value'], nested), oracle(['main', 'value'], nested));
  assert.equal(match.isMatch('value', nested), oracle.isMatch('value', nested));
  const overlong = 'a'.repeat(65537);
  assert.throws(() => match(['main'], overlong), /Input length/);
  assert.throws(() => match.isMatch('main', overlong), /Input length/);
});
