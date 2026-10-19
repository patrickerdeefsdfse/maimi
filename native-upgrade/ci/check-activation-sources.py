"""Require the tested cloud modules to equal the actual manager image sources."""
from pathlib import Path
import hashlib
import json

root = Path(__file__).resolve().parents[1]
candidate = root / "gateway/activation"
service = root.parent / "services/personal-cloud"
files = ("manager.py", "runtime_admission.py", "runtime_release.py", "runtime_snapshot.py")
checks = {}
for name in files:
    tested = (candidate / name).read_bytes()
    if tested != (service / name).read_bytes():
        raise SystemExit("ACTIVATION_SERVICE_SOURCE_MISMATCH: " + name)
    checks[name] = hashlib.sha256(tested).hexdigest()
print(json.dumps({"status": "MATCHED", "service_source_sha256": checks,
                  "company_requests": 0, "docker_requests": 0}))
