import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { Writable } from 'node:stream';
import { fileURLToPath } from 'node:url';
import semanticRelease from 'semantic-release';

const runtime = new URL('../', import.meta.url);
const repositoryUrl = new URL(process.argv[2]);
assert.equal(repositoryUrl.protocol, 'file:');
assert.equal(process.env.GIT_ALLOW_PROTOCOL, 'file');

for (const key of ['GITHUB_TOKEN', 'GH_TOKEN', 'NPM_TOKEN', 'NODE_AUTH_TOKEN', 'GIT_CREDENTIALS', 'GL_TOKEN', 'GITLAB_TOKEN']) {
  assert.equal(process.env[key], undefined);
}

const releaseConfig = createRequire(import.meta.url)('../../../release.config.cjs');
const { parserOpts, releaseRules } = await import('../analyze-commits.mjs');
const analyzerOptions = { parserOpts, releaseRules };
let output = '';
const publicationCalls = [];
const stream = new Writable({
  write(chunk, encoding, callback) {
    output += chunk.toString();
    callback();
  },
});

const result = await semanticRelease({
  branches: releaseConfig.branches,
  repositoryUrl: repositoryUrl.href,
  dryRun: true,
  ci: false,
  plugins: [
    [fileURLToPath(new URL('node_modules/@semantic-release/commit-analyzer/index.js', runtime)), analyzerOptions],
    fileURLToPath(new URL('node_modules/@semantic-release/release-notes-generator/index.js', runtime)),
    {
      publish() {
        publicationCalls.push('publish');
        throw new Error('Publication must never run in the local dry-run fixture');
      },
    },
  ],
}, {
  cwd: process.cwd(),
  env: process.env,
  stdout: stream,
  stderr: stream,
});

await writeFile('dry-run-result.json', JSON.stringify({ result, output, publicationCalls }));
