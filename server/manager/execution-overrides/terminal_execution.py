"""Read exact-Pod termination evidence before entering the normal release protocol."""


async def terminal_execution(manager, record):
    if (getattr(manager.store, 'role', None) != 'execution' or record.get('purpose') != 'worker'
            or record.get('status') not in ('ready', 'deleting') or not record.get('handle')):
        return False
    handle = manager.handle(record)
    provider = manager.provider
    expected = provider.manifests(provider.check_handle(handle))['pod']
    status, pod = await provider.call('GET', provider.base + '/pods/' + handle.name)
    if status != 200 or provider.verify(expected, pod, terminating=True) != handle.pod_uid:
        return False
    spec, state = pod['spec'], pod.get('status', {})
    if (spec.get('nodeName') != handle.node_name or spec.get('restartPolicy') != 'Never'
            or state.get('phase') not in ('Failed', 'Succeeded') or state.get('ephemeralContainerStatuses')):
        return False
    for spec_key, status_key in [('containers', 'containerStatuses'), ('initContainers', 'initContainerStatuses')]:
        names = {c['name'] for c in spec.get(spec_key, [])}
        observed = state.get(status_key, [])
        if len(observed) != len(names) or {c.get('name') for c in observed} != names:
            return False
        for container in observed:
            current = container.get('state', {})
            if set(current) != {'terminated'} or type(current['terminated'].get('exitCode')) is not int:
                return False
    return bool(spec.get('containers'))
