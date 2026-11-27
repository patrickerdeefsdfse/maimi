"""Upgrade a materialized desktop tree using integrity-checked official packages.

The existing Windows native dependencies and company resources stay in place.
Private desktop-host code is reused only with a recorded unchanged-source review.
Run rc2_shell.mjs afterwards to seal and verify the resulting runtime tree.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import tarfile

repo = Path(__file__).resolve().parents[1]
work = Path(os.environ["MAIMI_WORK"]).resolve(strict=True)
app = work / "windows/win-unpacked/resources/app"
runtime = app / "dsh"
pins = json.loads((repo / "deps/pins.json").read_text("utf8"))
version = pins["dsh"]["release"]
cache = Path(os.environ["MAIMI_DSH_PACKAGE_CACHE"]).resolve(strict=True)
manifest = json.loads(Path(os.environ["MAIMI_DSH_PACKAGE_MANIFEST"]).read_text("utf8"))
review = json.loads(Path(os.environ["MAIMI_DSH_SOURCE_REVIEW"]).read_text("utf8"))
assert review["sha256"] == pins["dsh"]["sha256"]
assert review["changes"]["apps/desktop-host/src/"] == []


def load(path):
    return json.loads(path.read_text("utf8"))


def write(path, data):
    # Materialized candidates may share hard links with a previous release.
    path.unlink(missing_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf8")


installed = []
packages = manifest["packages"]
for row in packages:
    assert row["version"] == version and row["package"].startswith("@deepseek-ai/dsh")
    archive = cache / row["file"]
    with archive.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == row["sha256"]
    destination = runtime / "node_modules" / row["package"]
    assert destination.resolve().is_relative_to(runtime.resolve())
    assert not destination.is_symlink() and not destination.is_junction()
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            assert member.name.startswith("package/") and not member.issym() and not member.islnk()
            member.name = member.name.removeprefix("package/")
            if member.name:
                bundle.extract(member, destination, filter="data")
    metadata = load(destination / "package.json")
    assert metadata["name"] == row["package"] and metadata["version"] == version
    installed.append({"name": row["package"], "version": version, "sha256": row["sha256"]})

host = runtime / "node_modules/@deepseek-ai/dsh-desktop-host/package.json"
data = load(host)
data["version"] = version
for section in ("dependencies", "devDependencies"):
    for name in data.get(section, {}):
        if name.startswith("@deepseek-ai/dsh"):
            data[section][name] = version
write(host, data)
metadata = load(runtime / "package.json")
metadata["version"] = version
for name in metadata["dependencies"]:
    if name.startswith("@deepseek-ai/dsh"):
        metadata["dependencies"][name] = version
for row in installed:
    metadata["dependencies"][row["name"]] = version
write(runtime / "package.json", metadata)
shell = load(app / "package.json")
for name in shell.get("dependencies", {}):
    if name.startswith("@deepseek-ai/dsh"):
        source = runtime / "node_modules" / name
        destination = app / "node_modules" / name
        assert destination.resolve().is_relative_to(app.resolve())
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
        shell["dependencies"][name] = version
for destination in (app / "node_modules/@deepseek-ai").iterdir():
    if destination.is_dir() and destination.name.startswith("dsh"):
        source = runtime / "node_modules/@deepseek-ai" / destination.name
        assert load(source / "package.json")["version"] == version
        if load(destination / "package.json")["version"] != version:
            assert destination.resolve().is_relative_to(app.resolve())
            shutil.rmtree(destination)
            shutil.copytree(source, destination)
write(app / "package.json", shell)
descriptor = load(runtime / "desktop-runtime.json")
descriptor["release"]["version"] = version
write(runtime / "desktop-runtime.json", descriptor)
write(work / "runtime-upgrade-packages.json", {"release": version, "packages": installed,
      "private_host": "unchanged implementation, version and dependency metadata synchronized",
      "source_review_sha256": review["sha256"], "runtime_seal": "requires rc2_shell.mjs"})
print(json.dumps({"release": version, "official_packages": len(installed), "status": "STAGED_REQUIRES_RESEAL"}))
