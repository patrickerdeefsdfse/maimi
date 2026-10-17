"""Independent static pilot boundary checks; never contact Docker or SSO."""
import copy
import json
from pathlib import Path, PurePosixPath
import sys
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

import pilot_deploy as deploy  # noqa: E402
from entry_app import Config  # noqa: E402


BINDINGS = [
    {"user_id": "8f9479b2-14d5-4495-8f7e-ff086313e179", "user_no": "00123"},
    {"user_id": "62ab14b7-4b2e-466e-966b-49879e312961", "user_no": "00124"},
]
BASE = PurePosixPath("/data/synthetic-pilot")


class PilotDeploymentBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.no_commands = patch.object(deploy, "execute", side_effect=AssertionError("unexpected command"))
        self.no_commands.start()
        self.addCleanup(self.no_commands.stop)
        self.bindings = copy.deepcopy(BINDINGS)
        self.document = deploy.compose_document(self.bindings, BASE)

    def test_persistent_volumes_follow_canonical_user_id_not_display_order(self):
        original = self.document["volumes"]
        reordered = deploy.compose_document(list(reversed(self.bindings)), BASE)["volumes"]
        for kind in ("state", "bootstrap"):
            self.assertEqual(original["a-" + kind]["name"], reordered["b-" + kind]["name"])
            self.assertEqual(original["b-" + kind]["name"], reordered["a-" + kind]["name"])
        names = {item["name"] for item in original.values()}
        self.assertEqual(len(names), 4)
        self.assertTrue(all(item["external"] is True for item in original.values()))
        for invalid in ("../other", "00000000-0000-0000-0000-000000000000", BINDINGS[0]["user_id"].upper()):
            with self.subTest(invalid=invalid), self.assertRaises((ValueError, deploy.Failure)):
                deploy.volume_names(invalid)

    def test_identity_config_preserves_leading_zeros_and_existing_uuids(self):
        config = deploy.configuration(self.bindings)
        parsed = Config.parse(config)
        for index, party in enumerate(("a", "b")):
            binding = config["bindings"][index]
            self.assertEqual(binding["user_no"], self.bindings[index]["user_no"])
            self.assertEqual(binding["user_id"], self.bindings[index]["user_id"])
            self.assertEqual(binding["upstream"], f"http://personal-{party}:3084")
            self.assertEqual(binding["bootstrap"], f"/run/megmeet-bootstrap/{party}/ready.json")
            self.assertEqual(binding["model_token_file"], f"/run/megmeet-entry/model-{party}")
        self.assertEqual(len(parsed.bindings), 2)
        self.assertEqual(self.bindings, BINDINGS)
        self.assertNotIn("api_key", json.dumps(config))

    def test_hosts_have_disjoint_network_and_writable_storage(self):
        for index, party in enumerate(("a", "b")):
            service = self.document["services"]["host-" + party]
            self.assertEqual(set(service["networks"]), {party})
            self.assertEqual(service["networks"][party]["aliases"], ["personal-" + party])
            mounts = {item["target"]: item for item in service["volumes"]}
            self.assertEqual(set(mounts), {"/state", "/run/megmeet-bootstrap", "/run/megmeet-config/model-token", "/opt/megmeet/native-entry"})
            self.assertEqual(mounts["/state"]["source"], party + "-state")
            self.assertFalse(mounts["/state"]["read_only"])
            self.assertEqual(mounts["/run/megmeet-bootstrap"]["source"], party + "-bootstrap")
            self.assertFalse(mounts["/run/megmeet-bootstrap"]["read_only"])
            self.assertEqual(mounts["/run/megmeet-config/model-token"]["source"], str(BASE / "private" / ("model-" + party)))
            self.assertTrue(mounts["/run/megmeet-config/model-token"]["read_only"])
            self.assertEqual(service["environment"]["MEGMEET_OWNER_ID"], self.bindings[index]["user_id"])
            raw = json.dumps(service)
            self.assertNotIn("docker.sock", raw)
            self.assertNotIn(str(deploy.SECRET), raw)
            self.assertNotIn("access_token", raw)
            self.assertNotIn("TOKENKEY_API_KEY", raw)

    def test_only_entry_can_read_both_bootstraps_and_company_key(self):
        entry = self.document["services"]["entry"]
        mounts = {item["target"]: item for item in entry["volumes"]}
        self.assertEqual(set(mounts), {"/opt/megmeet/native-entry", "/run/megmeet-entry", "/run/secrets/tokenkey-api-key", "/run/megmeet-bootstrap/a", "/run/megmeet-bootstrap/b"})
        self.assertTrue(all(item["read_only"] for item in mounts.values()))
        self.assertEqual(mounts["/run/secrets/tokenkey-api-key"]["source"], str(deploy.SECRET))
        for party in ("a", "b"):
            self.assertEqual(mounts["/run/megmeet-bootstrap/" + party]["source"], party + "-bootstrap")
            self.assertNotIn(party + "-state", {item["source"] for item in mounts.values()})
        self.assertEqual(set(entry["networks"]), {"a", "b", "common"})
        self.assertEqual(entry["networks"]["a"]["aliases"], ["native-entry"])
        self.assertEqual(entry["networks"]["b"]["aliases"], ["native-entry"])

    def test_private_networks_are_internal_and_common_net_entry_only(self):
        networks = self.document["networks"]
        for party in ("a", "b"):
            self.assertIs(networks[party]["internal"], True)
            self.assertEqual(networks[party]["name"], deploy.PROJECT + "-" + party)
            self.assertNotIn("external", networks[party])
        self.assertEqual(networks["common"], {"external": True, "name": "common-net"})
        for name, service in self.document["services"].items():
            self.assertNotIn("ports", service)
            if name != "entry":
                self.assertNotIn("common", service["networks"])

    def test_immutable_images_and_runtime_bounds(self):
        for name, service in self.document["services"].items():
            self.assertRegex(service["image"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(service["image"], deploy.ENTRY_IMAGE if name == "entry" else deploy.HOST_IMAGE)
            self.assertEqual(service["user"], "10001:10001")
            self.assertIs(service["read_only"], True)
            self.assertEqual(service["cap_drop"], ["ALL"])
            self.assertNotIn("cap_add", service)
            self.assertIn("no-new-privileges:true", service["security_opt"])
            self.assertLessEqual(service["pids_limit"], 256)
            self.assertEqual(service["memswap_limit"], service["mem_limit"])
            self.assertGreaterEqual(int(service["stop_grace_period"].removesuffix("s")), 20)
            self.assertEqual(service["labels"][deploy.LABEL], deploy.VERSION)
            self.assertNotIn("privileged", service)
        self.assertEqual(self.document["services"]["entry"]["mem_limit"], "384m")
        self.assertEqual(self.document["services"]["host-a"]["mem_limit"], "1280m")

    def test_mounts_never_create_paths_or_copy_image_files_into_private_volumes(self):
        for service in self.document["services"].values():
            for item in service["volumes"]:
                if item["type"] == "volume":
                    self.assertEqual(item["volume"], {"nocopy": True})
                else:
                    self.assertEqual(item["type"], "bind")
                    self.assertEqual(item["bind"], {"create_host_path": False})

    def test_entry_runs_real_application_without_synthetic_probe_or_access_logs(self):
        entry = self.document["services"]["entry"]
        self.assertEqual(entry["entrypoint"], ["python", "-B"])
        self.assertEqual(entry["command"][:3], ["-m", "uvicorn", "entry_app:app"])
        self.assertIn("--no-access-log", entry["command"])
        self.assertNotIn("probe_client.py", json.dumps(entry))
        self.assertEqual(entry["environment"], {"MEGMEET_ENTRY_CONFIG": "/run/megmeet-entry/config.json"})


if __name__ == "__main__":
    unittest.main()
