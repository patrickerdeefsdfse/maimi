import unittest
from dataclasses import replace
from pathlib import Path
import sys
from contextlib import nullcontext

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from maimi_sandbox.capacity import Commitment, Snapshot, Policy, CapacityAdmission, decide


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = Snapshot(100, 32768, 24000, 16000,
                                 (Commitment(1536, 1000, 6), Commitment(8192, 2000)), True, True)
        self.policy = Policy()
        self.request = Commitment(2048, 1000)

    def check(self, snapshot=None, **kwargs):
        return decide(snapshot or self.snapshot, policy=self.policy, requested=self.request,
                      reserved=kwargs.pop('reserved', ()), now=kwargs.pop('now', 101), **kwargs)

    def test_budget_deducts_existing_worker_office_and_platform(self):
        result = self.check()
        self.assertTrue(result.allowed)
        self.assertEqual(result.remaining_memory_mib, 9216)
        self.assertEqual(result.remaining_cpu_millis, 6000)

    def test_high_available_memory_does_not_override_legacy_commitments(self):
        snap = replace(self.snapshot, total_memory_mib=32768, available_memory_mib=24000,
                       legacy=(Commitment(1536, 1000, 14), Commitment(8192, 2000)))
        self.assertEqual(self.check(snap).reason, 'MEMORY_COMMITMENT_EXHAUSTED')

    def test_unknown_active_instance_remains_charged(self):
        self.assertEqual(self.check(reserved=(Commitment(10240, 1000),)).reason,
                         'MEMORY_COMMITMENT_EXHAUSTED')

    def test_available_pressure_is_independent_guard(self):
        self.assertEqual(self.check(replace(self.snapshot, available_memory_mib=3000)).reason,
                         'HOST_MEMORY_PRESSURE')

    def test_cpu_commitments_cannot_use_memory_headroom(self):
        self.assertEqual(self.check(replace(self.snapshot, total_cpu_millis=8500)).reason,
                         'CPU_COMMITMENT_EXHAUSTED')

    def test_incomplete_or_changed_inventory_denied(self):
        for field in ('inventory_complete', 'identities_stable'):
            self.assertEqual(self.check(replace(self.snapshot, **{field: False})).reason,
                             'NODE_INVENTORY_UNVERIFIED')

    def test_future_and_stale_sample_denied(self):
        for now in (99, 111):
            self.assertEqual(self.check(now=now).reason, 'NODE_SAMPLE_STALE')

    def test_non_finite_and_bool_resource_inputs_denied(self):
        for now in (float('nan'), float('inf'), True, 10**1000):
            self.assertEqual(self.check(now=now).reason, 'INVALID_CAPACITY_INPUT')
        self.assertEqual(self.check(replace(self.snapshot, total_memory_mib=True)).reason,
                         'INVALID_CAPACITY_INPUT')

    def test_oversized_clock_values_fail_closed(self):
        self.assertEqual(self.check(replace(self.snapshot, sampled_monotonic=10**1000)).reason,
                         'INVALID_CAPACITY_INPUT')
        self.policy = replace(self.policy, maximum_sample_age_seconds=10**1000)
        self.assertEqual(self.check().reason, 'INVALID_CAPACITY_INPUT')

    def test_concrete_port_requires_lock_and_samples_each_attempt(self):
        samples = []
        def sample():
            samples.append(True)
            return self.snapshot
        port = CapacityAdmission(lock=nullcontext, snapshot=sample, clock=lambda: 101)
        self.assertFalse(port.check(reserved=(), requested=self.request).allowed)
        self.assertEqual(samples, [])
        with port.locked():
            self.assertTrue(port.check(reserved=(), requested=self.request).allowed)
        self.snapshot = replace(self.snapshot, inventory_complete=False)
        with port.locked():
            self.assertEqual(port.check(reserved=(), requested=self.request).reason,
                             'NODE_INVENTORY_UNVERIFIED')
        self.assertEqual(len(samples), 2)


if __name__ == '__main__':
    unittest.main()
