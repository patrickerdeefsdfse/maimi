// The authenticated ready response binds the endpoint to one Kubernetes Pod.
// Docker workers keep the historical response when both variables are absent.
export function readWorkerIdentity(env) {
  const pod = env.CLOUD_POD_UID || '';
  const raw = env.CLOUD_WORKER_GENERATION || '';
  if (!pod && !raw) return Object.freeze({});
  if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(pod)
      || !/^[1-9][0-9]{0,9}$/.test(raw)) throw Error('cloud worker identity invalid');
  const generation = Number(raw);
  if (!Number.isSafeInteger(generation) || generation > 2147483647)
    throw Error('cloud worker identity invalid');
  return Object.freeze({pod_uid: pod, generation});
}

export function workerReady(nativeWorkspaceId, identity) {
  return {status: 'ready', native_workspace_id: nativeWorkspaceId, path: '/workspace', ...identity};
}
