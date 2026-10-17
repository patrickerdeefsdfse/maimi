"""Conservative same-host admission arithmetic over trusted node observations.

This is an internal control-plane input, never a model-supplied resource claim.
It reserves configured legacy limits and keeps observed free memory as a second
pressure check. It cannot coordinate two schedulers by itself: the controller
must serialize this check with reservation and legacy workspace admission.
"""
from dataclasses import dataclass
from contextlib import contextmanager
import math
import time


@dataclass(frozen=True)
class Commitment:
    memory_mib: int
    cpu_millis: int
    count: int = 1


@dataclass(frozen=True)
class Snapshot:
    sampled_monotonic: float
    total_memory_mib: int
    available_memory_mib: int
    total_cpu_millis: int
    legacy: tuple[Commitment, ...]
    inventory_complete: bool
    identities_stable: bool


@dataclass(frozen=True)
class Policy:
    platform_memory_mib: int = 4096
    platform_cpu_millis: int = 1000
    minimum_available_mib: int = 2048
    maximum_sample_age_seconds: float = 10


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    remaining_memory_mib: int = 0
    remaining_cpu_millis: int = 0


class CapacityAdmission:
    """Trusted port; the host-wide lock and inventory reader are not model inputs.

    The caller must supply a bounded lock shared with legacy workspace admission
    and a bounded snapshot reader. The legacy scheduler must read the sandbox
    reservations while holding the same lock; merely constructing this class
    does not establish that cross-service contract.
    """
    def __init__(self, *, lock, snapshot, policy=Policy(), clock=time.monotonic):
        self._lock, self._snapshot, self.policy, self.clock = lock, snapshot, policy, clock
        self._held = False

    @contextmanager
    def locked(self):
        if self._held:
            raise RuntimeError('HOST_ADMISSION_LOCK_REENTERED')
        with self._lock():
            self._held = True
            try:
                yield
            finally:
                self._held = False

    def check(self, *, reserved, requested):
        if not self._held:
            return Decision(False, 'HOST_ADMISSION_LOCK_REQUIRED')
        return decide(self._snapshot(), policy=self.policy, reserved=reserved,
                      requested=requested, now=self.clock())


def _integer(value, *, positive=False):
    return type(value) is int and value >= (1 if positive else 0)


def _finite_time(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _commitment(value):
    return (isinstance(value, Commitment)
            and _integer(value.memory_mib, positive=True)
            and _integer(value.cpu_millis, positive=True)
            and _integer(value.count, positive=True))


def decide(snapshot, *, policy, reserved, requested, now):
    """READY, BUSY, provisioning and unknown instances must all be in reserved.

    Missing/unlimited legacy resource limits must be resolved into explicit
    administrator reservations before inventory_complete can be true. In this
    first phase CPU and memory are both conservative configured commitments.
    """
    if not isinstance(snapshot, Snapshot) or not isinstance(policy, Policy):
        return Decision(False, 'INVALID_CAPACITY_INPUT')
    if (not _integer(snapshot.total_memory_mib, positive=True)
        or not _integer(snapshot.available_memory_mib)
        or snapshot.available_memory_mib > snapshot.total_memory_mib
        or not _integer(snapshot.total_cpu_millis, positive=True)
        or not _integer(policy.platform_memory_mib, positive=True)
        or not _integer(policy.platform_cpu_millis, positive=True)
        or not _integer(policy.minimum_available_mib, positive=True)
        or not _finite_time(now)
        or not _finite_time(snapshot.sampled_monotonic)
        or not _finite_time(policy.maximum_sample_age_seconds)
        or policy.maximum_sample_age_seconds <= 0
        or not isinstance(snapshot.legacy, tuple) or not isinstance(reserved, tuple)
        or not all(_commitment(c) for c in (*snapshot.legacy, *reserved))
        or not _commitment(requested) or requested.count != 1):
        return Decision(False, 'INVALID_CAPACITY_INPUT')
    if snapshot.inventory_complete is not True or snapshot.identities_stable is not True:
        return Decision(False, 'NODE_INVENTORY_UNVERIFIED')
    age = now - snapshot.sampled_monotonic
    if age < 0 or age > policy.maximum_sample_age_seconds:
        return Decision(False, 'NODE_SAMPLE_STALE')
    commitments = (*snapshot.legacy, *reserved)
    memory = snapshot.total_memory_mib - policy.platform_memory_mib - sum(c.memory_mib*c.count for c in commitments)
    cpu = snapshot.total_cpu_millis - policy.platform_cpu_millis - sum(c.cpu_millis*c.count for c in commitments)
    remaining = {'remaining_memory_mib': max(0, memory), 'remaining_cpu_millis': max(0, cpu)}
    if requested.memory_mib > memory:
        return Decision(False, 'MEMORY_COMMITMENT_EXHAUSTED', **remaining)
    if requested.cpu_millis > cpu:
        return Decision(False, 'CPU_COMMITMENT_EXHAUSTED', **remaining)
    if snapshot.available_memory_mib - requested.memory_mib < policy.minimum_available_mib:
        return Decision(False, 'HOST_MEMORY_PRESSURE', **remaining)
    return Decision(True, 'ADMITTED', memory-requested.memory_mib, cpu-requested.cpu_millis)
