import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';

const require = createRequire(import.meta.url);
const file = join(dirname(require.resolve('semantic-release')), 'lib/get-next-version.js');
const originalHash = '023e1aaa80ef7639b572d41949693b47e64f245fcdcda9393640ca5b8d4845ad';
const patchedHash = 'c32f90068fc49aa117801d3f48d7ad4e4163179c3c147e1e2240ad60b899ea4a';
const source = readFileSync(file, 'utf8');
const sourceHash = createHash('sha256').update(source).digest('hex');
if (sourceHash !== patchedHash) {
  if (sourceHash !== originalHash) {
    throw new Error('Semantic Release version calculator changed. Review the collision adapter before updating the pinned runtime.');
  }
  const patched = source
    .replace('import semver from "semver";', 'import semver from "semver";\nimport { execFileSync } from "node:child_process";')
    .replace('{ getLatestVersion, highest, isSameChannel, tagsToVersions }', '{ getLatestVersion, highest, isSameChannel, tagsToVersions, makeTag }')
    .replace('export default ({ branch,', 'export default ({ cwd, env, options, branch,')
    .replace('  return version;', `  const occupiedReleaseTags = new Set([
    ...execFileSync('git', ['tag', '--list'], { cwd, env, encoding: 'utf8' }).split('\\n'),
    ...JSON.parse(env.SDK_RELEASE_EXISTING_TAGS || '[]'),
  ]);
  while (occupiedReleaseTags.has(makeTag(options.tagFormat, version))) {
    version = semver.inc(version, semver.prerelease(version) ? 'prerelease' : 'patch');
    logger.log('Existing release version preserved; advancing to %s', version);
  }
  return version;`);
  if (createHash('sha256').update(patched).digest('hex') !== patchedHash) throw new Error('Unexpected collision adapter transformation.');
  writeFileSync(file, patched);
}
