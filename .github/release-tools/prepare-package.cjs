const { readFileSync, writeFileSync, mkdirSync, readdirSync } = require('node:fs');
const { execFileSync } = require('node:child_process');

function pythonVersion(version) {
  return version.replace(/-alpha\.(\d+)$/, 'a$1').replace(/-beta\.(\d+)$/, 'b$1').replace(/-dev\.(\d+)$/, '.dev$1');
}

function verifyRelease({ language }, { cwd, nextRelease }) {
  if (language !== 'go') return;
  const modulePath = readFileSync(`${cwd}/go.mod`, 'utf8').match(/^module\s+(\S+)/m)?.[1];
  const major = Number(nextRelease.version.split('.')[0]);
  const suffix = Number(modulePath?.match(/\/v(\d+)$/)?.[1] || 0);
  if (!modulePath || (major >= 2 && suffix !== major) || (major < 2 && suffix >= 2)) {
    throw new Error(`Go release ${nextRelease.version} requires a matching module major-version suffix in go.mod before it can be tagged.`);
  }
}

function commitVersion({ cwd, branch, options, nextRelease }, files) {
  const git = args => execFileSync('git', args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
  git(['add', '--', ...files]);
  if (!git(['diff', '--cached', '--name-only', '--', ...files]).trim()) return;
  git(['-c', 'user.name=github-actions[bot]', '-c', 'user.email=41898282+github-actions[bot]@users.noreply.github.com',
    'commit', '-m', `chore(release): ${nextRelease.version} [skip ci]`, '--', ...files]);
  try {
    git(['push', options.repositoryUrl, `HEAD:refs/heads/${branch.name}`]);
  } catch {
    throw new Error('Release metadata could not be pushed without force. Check branch policy and concurrent updates; no tag was created.');
  }
}

function buildEnv(env = process.env) {
  return Object.fromEntries(Object.entries(env).filter(([key]) => !/(TOKEN|SECRET|PASSWORD|WEBHOOK|AUTHORIZATION|CREDENTIAL)/i.test(key) && !key.startsWith('GIT_')));
}

function setPythonVersion(cwd, version) {
  const files = [
    [`${cwd}/pyproject.toml`, /^version = ".*"$/gm, `version = "${version}"`],
    [`${cwd}/src/envai/__init__.py`, /^__version__ = ".*"$/gm, `__version__ = "${version}"`],
  ];
  const replacements = files.map(([file, pattern, replacement]) => {
    const original = readFileSync(file, 'utf8');
    if ([...original.matchAll(pattern)].length !== 1) throw new Error('Expected exactly one Python version assignment in each release metadata file.');
    return [file, original.replace(pattern, replacement)];
  });
  for (const [file, content] of replacements) writeFileSync(file, content);
}

async function prepare({ language }, context) {
  const { cwd, nextRelease } = context;
  const run = (command, args) => execFileSync(command, args, { cwd, stdio: 'inherit', env: buildEnv() });
  mkdirSync(`${cwd}/release-assets`, { recursive: true });
  if (readdirSync(`${cwd}/release-assets`).length) throw new Error('Release output directory must be empty before packaging.');
  if (language === 'typescript') {
    run('npm', ['version', nextRelease.version, '--no-git-tag-version', '--ignore-scripts', '--allow-same-version']);
    run('npm', ['run', 'check:package']);
    run('npm', ['pack', '--pack-destination', 'release-assets']);
    commitVersion(context, ['package.json', 'package-lock.json']);
  } else if (language === 'python') {
    const version = pythonVersion(nextRelease.version);
    setPythonVersion(cwd, version);
    run('python', ['-m', 'build', '--outdir', 'release-assets']);
    run('python', ['-m', 'twine', 'check', '--strict', ...readdirSync(`${cwd}/release-assets`).map(name => `release-assets/${name}`)]);
    run('python', [require('node:path').join(__dirname, 'validate-python-package.py'), 'release-assets', version]);
    commitVersion(context, ['pyproject.toml', 'src/envai/__init__.py']);
  }
}

module.exports = { prepare, verifyRelease, pythonVersion, commitVersion, buildEnv, setPythonVersion };
