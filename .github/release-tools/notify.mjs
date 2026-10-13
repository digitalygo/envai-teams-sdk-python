export function notification({ outcome, repository, tag, commit, runUrl }) {
  const emoji = outcome === 'success' ? '✅' : '❌';
  return `${emoji} ${repository} | release ${tag || 'not created'} | commit ${commit} | outcome ${outcome} | ${runUrl}`;
}

export async function notify(env = process.env, send = fetch) {
  if (!env.KCHAT_WEBHOOK_URL) {
    console.log('kChat notification skipped: KCHAT_WEBHOOK_URL is not configured.');
    return { skipped: true };
  }
  let destination;
  try { destination = new URL(env.KCHAT_WEBHOOK_URL); } catch {
    throw new Error('KCHAT_WEBHOOK_URL must be a valid HTTPS URL.');
  }
  if (destination.protocol !== 'https:' || destination.username || destination.password) {
    throw new Error('KCHAT_WEBHOOK_URL must be a valid HTTPS URL.');
  }
  const text = notification({
    outcome: env.RELEASE_OUTCOME,
    repository: env.GITHUB_REPOSITORY,
    tag: env.RELEASE_TAG,
    commit: env.RELEASE_COMMIT || env.GITHUB_SHA,
    runUrl: `${env.GITHUB_SERVER_URL}/${env.GITHUB_REPOSITORY}/actions/runs/${env.GITHUB_RUN_ID}`,
  });
  let response;
  try {
    response = await send(destination, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ text }), redirect: 'error', signal: AbortSignal.timeout(15000),
    });
  } catch { throw new Error('kChat request failed; webhook details were not logged.'); }
  if (!response.ok) throw new Error(`kChat notification failed with HTTP ${response.status}.`);
  console.log('kChat release notification sent.');
  return { skipped: false };
}

if (process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href) {
  await notify();
}
