"""Explicit execution budgets, separate from chat/session counts.

Profiles are release-pinned: change them only after draining the old release.
Memory admission charges the limit plus runtime headroom, never just requests.
PVC owners remain on their original node; this is expansion, not failover.
"""
from dataclasses import dataclass
import json
import re


@dataclass(frozen=True)
class Resources:
    cpu: int
    cpu_limit: int
    memory: int
    overhead: int
    ephemeral: int

    @property
    def charged_memory(self):
        return self.memory + self.overhead


PROFILES = {
    'chat': Resources(250, 1000, 896, 96, 512),
    'light': Resources(250, 1000, 512, 64, 512),
    'standard': Resources(500, 2000, 768, 96, 1024),
    'office': Resources(1000, 4000, 2048, 128, 2048),
}
RANK = {'legacy': 0, 'chat': 0, 'light': 1, 'standard': 2, 'office': 3}


@dataclass(frozen=True)
class NodeBudget:
    name: str
    cpu: int
    memory: int
    ephemeral: int
    slots: int
    starting: int = 3
    observer_url: str = ''
    observer_key_file: str = ''

    def __post_init__(self):
        if not isinstance(self.name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}', self.name):
            raise ValueError('POOL_NODE_INVALID')
        if any(type(v) is not int or v <= 0 for v in (self.cpu,self.memory,self.ephemeral,self.slots,self.starting)):
            raise ValueError('POOL_BUDGET_INVALID')
        # /26 node CIDR has a small pod budget; leave system/maintenance headroom.
        if self.slots > 48 or self.starting > 8:
            raise ValueError('POOL_NODE_POD_BUDGET_EXCEEDED')


@dataclass(frozen=True)
class ExecutionPool:
    nodes: tuple

    def __post_init__(self):
        if not self.nodes or len(self.nodes) > 16 or len({n.name for n in self.nodes}) != len(self.nodes):
            raise ValueError('POOL_NODES_INVALID')

    def node(self, name):
        for node in self.nodes:
            if node.name == name:
                return node
        raise ValueError('OWNER_HOME_NODE_NOT_IN_POOL')

    @classmethod
    def load(cls, path):
        # Deployment-owned JSON contains budgets/observer endpoints, never secrets.
        with open(path, encoding='utf-8') as stream:
            value = json.load(stream)
        if set(value) != {'schema','nodes'} or value['schema'] != 'maimi-execution-pool/v1':
            raise ValueError('POOL_CONFIGURATION_INVALID')
        return cls(tuple(NodeBudget(**node) for node in value['nodes']))

    def summary(self):
        return [{'node': n.name, 'maximum_hosts': n.slots, 'cpu_millis': n.cpu,
                 'memory_mib': n.memory, 'ephemeral_mib': n.ephemeral,
                 'profiles': {k: min(n.slots,n.cpu//r.cpu,n.memory//r.charged_memory,n.ephemeral//r.ephemeral)
                              for k,r in PROFILES.items()}} for n in self.nodes]


class PoolObserver:
    """Each node has its own authenticated stop observer and replay cache."""
    def __init__(self, clients):
        self.clients = clients

    def client(self, handle):
        node = handle.node_name if hasattr(handle,'node_name') else handle['node_name']
        if node not in self.clients:
            raise ValueError('TERMINATION_OBSERVER_NOT_CONFIGURED')
        return self.clients[node]

    async def __call__(self, handle):
        return await self.client(handle)(handle)

    async def verify(self, record, proof):
        return await self.client(record['handle']).verify(record, proof)
