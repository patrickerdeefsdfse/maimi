"""Verify the exact owned release ZIP, then rehearse it offline in MegmeetDev."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from zipfile import ZipFile

if os.environ.get("WSL_DISTRO_NAME") != "MegmeetDev" or os.geteuid() != 0:
    raise SystemExit("Only the local owned MegmeetDev environment is supported")
root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--revision', choices=('r6', 'r7', 'r8', 'r9'), default='r9')
revision = parser.parse_args().revision
package = root / f"releases/MEGMEET-Changsha-Personal-Cloud-20260918-{revision}.zip"
expected = package.with_suffix(".zip.sha256").read_text().split()[0]
with package.open("rb") as stream:
    actual = hashlib.file_digest(stream, "sha256").hexdigest()
if actual != expected:
    raise SystemExit("Outer ZIP checksum mismatch")
print(f"FINAL_{revision.upper()}_PACKAGE_SHA256:", actual, flush=True)
stage = Path(tempfile.mkdtemp(prefix="megmeet-final-release-")).resolve()
try:
    with ZipFile(package) as archive:
        entries = archive.namelist()
        manifest = json.loads(archive.read("release-manifest.json"))
        allowed = set(manifest["files"]) | {
            "release-manifest.json", "images-manifest.json", "images.tar.gz", "INSTALL.md"
        }
        if set(entries) != allowed or len(entries) != len(set(entries)):
            raise SystemExit("Unexpected or duplicate ZIP entries")
        for name in entries:
            target = (stage / name).resolve()
            if stage not in target.parents:
                raise SystemExit("ZIP path outside temporary release directory")
            if name == "images.tar.gz":
                image_manifest = json.loads(archive.read("images-manifest.json"))
                with archive.open(name) as stream:
                    image_sha = hashlib.file_digest(stream, "sha256").hexdigest()
                if image_sha != image_manifest["archive"]["sha256"]:
                    raise SystemExit("Offline image archive checksum mismatch")
                continue
            contents = archive.read(name)
            if name in manifest["files"]:
                record = manifest["files"][name]
                if len(contents) != record["bytes"] or hashlib.sha256(contents).hexdigest() != record["sha256"]:
                    raise SystemExit("Packaged source checksum mismatch")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(contents)
    print(f"FINAL_{revision.upper()}_ARCHIVE_SOURCE_AND_IMAGES: PASS", flush=True)
    result = subprocess.run([
        sys.executable, str(root / "dev-local/rehearse_changsha.py"),
        "--release-directory", str(stage),
    ], timeout=1200)
    if result.returncode:
        raise SystemExit(result.returncode)
    print(f"EXACT_{revision.upper()}_PACKAGE_REHEARSAL: PASS", flush=True)
finally:
    if stage.parent == Path("/tmp") and stage.name.startswith("megmeet-final-release-"):
        shutil.rmtree(stage)
