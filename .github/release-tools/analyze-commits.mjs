import { analyzeCommits as analyze } from '@semantic-release/commit-analyzer';

export const parserOpts = {
  headerPattern: /^(\w*)(?:\((.*)\))?!?: (.*)$/,
  breakingHeaderPattern: /^(\w*)(?:\((.*)\))?!: (.*)$/,
  headerCorrespondence: ['type', 'scope', 'subject'],
  noteKeywords: ['BREAKING CHANGE', 'BREAKING CHANGES'],
};

export const releaseRules = [
  { type: '*', release: false },
  { type: 'feat', release: 'minor' },
  { type: 'fix', release: 'patch' },
  { type: 'feat', breaking: true, release: 'major' },
  { type: 'fix', breaking: true, release: 'major' },
  ...['chore', 'refactor', 'build'].map(type => ({ type, release: 'patch' })),
];

export async function analyzeCommits(_, context) {
  return analyze({ parserOpts, releaseRules }, {
    ...context,
    commits: context.commits.filter(commit => !commit.message.includes('[skip ci]')),
  });
}
