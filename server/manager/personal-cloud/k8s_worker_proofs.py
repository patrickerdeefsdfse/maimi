"""Generation-bound routing and authenticated native readiness, never termination."""
import asyncio
import ipaddress
import json
import re
import urllib.error
import urllib.request

from k8s_worker_provider import NoRedirect, ProviderError, require, uid


def endpoint_slice_proof(handle, result, pod_ip):
    require(isinstance(result, dict) and isinstance(result.get('items'), list) and
            not result.get('metadata', {}).get('continue'), 'ENDPOINT_LIST_REFUSED')
    require(len(result['items']) <= 200, 'ENDPOINT_LIST_REFUSED')
    try:
        ip = ipaddress.ip_address(pod_ip)
        require(not (ip.is_loopback or ip.is_multicast or ip.is_unspecified), 'POD_IP_REFUSED')
    except ValueError:
        raise ProviderError('POD_IP_REFUSED') from None
    ready = set()
    slice_uids = set()
    for item in result['items']:
        require(isinstance(item, dict), 'ENDPOINT_IDENTITY_REFUSED')
        meta = item.get('metadata', {})
        owners = meta.get('ownerReferences', [])
        require(meta.get('namespace') == handle.namespace and uid(meta.get('uid')) and
                not meta.get('deletionTimestamp') and meta.get('labels', {}).get('kubernetes.io/service-name') == handle.name and
                len(owners) == 1 and owners[0].get('apiVersion') == 'v1' and owners[0].get('kind') == 'Service' and
                owners[0].get('name') == handle.name and owners[0].get('uid') == handle.service_uid and
                owners[0].get('controller') is True, 'ENDPOINT_SERVICE_UID_REFUSED')
        ports = item.get('ports', [])
        require(len(ports) == 1 and ports[0].get('name') == 'worker' and ports[0].get('port') == 3084 and
                ports[0].get('protocol') == 'TCP', 'ENDPOINT_PORT_REFUSED')
        require(item.get('addressType') == ('IPv4' if ip.version == 4 else 'IPv6'), 'ENDPOINT_ADDRESS_REFUSED')
        for endpoint in item.get('endpoints', []):
            target = endpoint.get('targetRef', {})
            require(target.get('kind') == 'Pod' and target.get('namespace') == handle.namespace and
                    target.get('name') == handle.name and target.get('uid') == handle.pod_uid and
                    endpoint.get('nodeName') == handle.node_name and endpoint.get('addresses') == [pod_ip],
                    'ENDPOINT_POD_UID_REFUSED')
            conditions = endpoint.get('conditions', {})
            if conditions.get('ready') is True and conditions.get('terminating') is not True and conditions.get('serving') is not False:
                ready.add((handle.pod_uid, pod_ip, handle.node_name))
                slice_uids.add(meta['uid'])
    if not ready:
        return None
    require(len(ready) == 1, 'ENDPOINT_NOT_UNIQUE')
    return {'pod_uid': handle.pod_uid, 'pod_ip': pod_ip, 'node_name': handle.node_name,
            'service_uid': handle.service_uid, 'slice_uids': sorted(slice_uids)}


class WorkerHTTP:
    """Only one fixed native readiness path; no inherited proxy or redirect."""
    def __init__(self):
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    async def __call__(self, origin, token):
        return await asyncio.to_thread(self._request, origin, token)

    def _request(self, origin, token):
        require(isinstance(origin, str) and re.fullmatch(r'http://mmw-[0-9a-f]{32}-g[1-9][0-9]{0,9}:3084', origin), 'WORKER_ORIGIN_REFUSED')
        require(isinstance(token, str) and re.fullmatch(r'[A-Za-z0-9_-]{43}', token), 'PRIVATE_ROUTE_CREDENTIAL_REFUSED')
        try:
            request = urllib.request.Request(origin + '/__cloud/ready', headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
            with self.opener.open(request, timeout=5) as response:
                require(response.code == 200, 'NATIVE_WORKER_NOT_READY')
                raw = response.read(16385)
            require(len(raw) <= 16384, 'NATIVE_WORKER_RESPONSE_REFUSED')
            result = json.loads(raw)
            require(isinstance(result, dict), 'NATIVE_WORKER_RESPONSE_REFUSED')
            return result
        except ProviderError:
            raise
        except Exception:
            raise ProviderError('NATIVE_WORKER_NOT_READY') from None


async def authenticated_ready(handle, state, route_token, http=None):
    require(isinstance(route_token, str) and re.fullmatch(r'[A-Za-z0-9_-]{43}', route_token), 'PRIVATE_ROUTE_CREDENTIAL_REFUSED')
    require(state.get('ready') is True and state.get('endpoint', {}).get('pod_uid') == handle.pod_uid and
            state.get('origin') == 'http://' + handle.name + ':3084', 'NATIVE_ROUTE_PROOF_REFUSED')
    value = await (http or WorkerHTTP())(state['origin'], route_token)
    require(isinstance(value, dict) and value.get('pod_uid') == handle.pod_uid and
            type(value.get('generation')) is int and value['generation'] == handle.generation, 'NATIVE_WORKER_GENERATION_REFUSED')
    result = {'pod_uid': handle.pod_uid, 'generation': handle.generation, 'origin': state['origin']}
    if handle.mode == 'worker':
        require(value.get('status') == 'ready' and value.get('path') == '/workspace' and
                isinstance(value.get('native_workspace_id'), str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value['native_workspace_id']),
                'NATIVE_WORKER_NOT_READY')
        result['native_workspace_id'] = value['native_workspace_id']
    else:
        require(value.get('mode') == ('card-retention-v1' if handle.mode=='card' else 'source-retention-v1'), 'MAINTENANCE_HELPER_NOT_READY')
        result['mode'] = value['mode']
    return result
