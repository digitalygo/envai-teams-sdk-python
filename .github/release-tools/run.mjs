import { appendFileSync } from 'node:fs';
import semanticRelease from 'semantic-release';

if (!process.env.GITHUB_TOKEN) throw new Error('Missing required repository/organization secret RELEASE_TOKEN.');
if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(process.env.GITHUB_REPOSITORY || '')) {
  throw new Error('Missing valid release repository identity.');
}
const existingTags = [];
for (let page = 1; ; page++) {
  const response = await fetch(`https://api.github.com/repos/${process.env.GITHUB_REPOSITORY}/releases?per_page=100&page=${page}`, {
    headers: { authorization: `Bearer ${process.env.GITHUB_TOKEN}`, accept: 'application/vnd.github+json' },
    redirect: 'error', signal: AbortSignal.timeout(30000),
  });
  if (!response.ok) throw new Error(`Cannot verify existing GitHub releases: HTTP ${response.status}.`);
  const releases = await response.json();
  if (!Array.isArray(releases)) throw new Error('Invalid release-list response.');
  existingTags.push(...releases.map(release => release.tag_name));
  if (releases.length < 100) break;
}
process.env.SDK_RELEASE_EXISTING_TAGS = JSON.stringify(existingTags);
const result = await semanticRelease();
if (result?.nextRelease && process.env.GITHUB_OUTPUT) {
  appendFileSync(process.env.GITHUB_OUTPUT, `released=true\ntag=${result.nextRelease.gitTag}\ncommit=${result.nextRelease.gitHead}\n`);
}
