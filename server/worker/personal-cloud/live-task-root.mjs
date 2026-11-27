// Scope a managed capability to the exact resident agent and its runtime-owned root.
export function liveTaskRoot(agents, subagents, agent) {
  if (!agent || agents.get(agent.id) !== agent) return undefined;
  try {
    const root = subagents.rootOf(agent);
    if (root && agents.get(root.id) === root && root.session.id === root.id) return root;
  } catch {
    // A retiring delegation can remain visible while its runtime ownership is gone.
  }
  return undefined;
}
