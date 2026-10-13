import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile, rm } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createGitFixture, execute } from './fixtures.mjs';

for (const [branch, version] of [['main', '1.0.1'], ['beta', '1.0.1-beta.1'], ['alpha', '1.0.1-alpha.1']]) {
  test(`semantic-release dry-run computes ${version} without publication`, { timeout: 60000 }, async (context) => {
    const fixture = await createGitFixture(branch);
    context.after(() => rm(fixture.root, { recursive: true, force: true }));
    const remoteRefs = async () => (await execute('git', ['--git-dir', fixture.remote, 'show-ref'], {
      cwd: fixture.cwd,
      env: fixture.env,
    })).stdout;
    const localReleaseRefs = () => fixture.git('for-each-ref', '--format=%(refname):%(objectname)', 'refs/tags', 'refs/notes');
    const beforeRemote = await remoteRefs();
    const beforeLocal = await localReleaseRefs();
    await execute(process.execPath, [
      fileURLToPath(new URL('semantic-release-dry-run.mjs', import.meta.url)),
      pathToFileURL(fixture.remote).href,
    ], {
      cwd: fixture.cwd,
      env: fixture.env,
      timeout: 45000,
    });
    const { result, output, publicationCalls } = JSON.parse(await readFile(`${fixture.cwd}/dry-run-result.json`, 'utf8'));

    assert.equal(result.nextRelease.version, version);
    assert.equal(result.nextRelease.type, 'patch');
    assert.equal(result.nextRelease.gitTag, `v${version}`);
    assert.deepEqual(publicationCalls, []);
    assert.match(output, /dry-run mode/);
    assert.match(output, /Skip .* tag creation in dry-run mode/);
    assert.match(output, /Skip step "publish" of plugin "Inline plugin" in dry-run mode/);
    assert.equal(await remoteRefs(), beforeRemote);
    assert.equal(await localReleaseRefs(), beforeLocal);
    assert.equal(fixture.env.GITHUB_TOKEN, undefined);
    assert.equal(fixture.env.NPM_TOKEN, undefined);
    assert.equal(fixture.env.GIT_ALLOW_PROTOCOL, 'file');
  });
}
