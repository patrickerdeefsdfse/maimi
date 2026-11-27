"""Materialize a portable Windows app without importing development machine links."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
BUILD = Path(os.environ["MAIMI_WORK"]).resolve()
PREPARED = BUILD / "prepared/windows"
APP = REPO / "shell"
STAGE = BUILD / "windows/win-unpacked"
RESOURCE = STAGE / "resources"
PRODUCT = RESOURCE / "app"
FEED = "https://desktop-gateway.example.invalid/maimi/updates/windows/"
RELEASE = "0.2.2-rc.1"

def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


DEBUG_METADATA = (".js.map", ".cjs.map", ".mjs.map", ".d.ts.map", ".d.cts.map", ".d.mts.map", ".tsbuildinfo")


def copy_tree(source, dest, skip=(), omit_debug_metadata=False):
    """Only regular directories and files, preserving bytes. Never follow junctions."""
    if source.is_symlink() or source.is_junction():
        raise ValueError(f"linked source root: {source}")
    def ignored(directory, names):
        omitted = set(skip).intersection(names)
        if omit_debug_metadata:
            omitted.update(name for name in names if name.endswith(DEBUG_METADATA))
        for name in set(names) - omitted:
            entry = Path(directory) / name
            if entry.is_symlink() or entry.is_junction():
                raise ValueError(f"unexpected bundled link: {entry}")
        return omitted
    shutil.copytree(source, dest, ignore=ignored, dirs_exist_ok=True)


def shell_deps(product=PRODUCT):
    # Project installation uses pnpm's isolated linker. Flatten identical versions and
    # place conflicting versions next to the consumer, following Node's normal lookup.
    installed = {}
    modules = product / "node_modules"
    def resolve(start, name):
        for directory in (start, *start.parents):
            path = directory / "node_modules" / name
            if (path / "package.json").is_file():
                return path.resolve()
        return None
    def visit(name, source, consumer):
        meta = read(source / "package.json")
        for ancestor in (consumer, *consumer.parents):
            target = ancestor / "node_modules" / name
            if target in installed:
                if installed[target] == source:
                    return
                break
            if ancestor == product:
                break
        target = modules / name
        if target in installed or (name in top and top[name] != source):
            target = consumer / "node_modules" / name
        if target in installed:
            raise ValueError(f"conflicting dependency: {name}")
        installed[target] = source
        copy_tree(source, target, ("node_modules",))
        for dep in dict.fromkeys([*meta.get("dependencies",{}), *meta.get("optionalDependencies",{}), *meta.get("peerDependencies",{})]):
            resolved = resolve(source, dep)
            if resolved:
                visit(dep, resolved, target)
            elif dep in meta.get("dependencies",{}) and dep not in meta.get("optionalDependencies",{}):
                raise ValueError(f"{name}: required dependency {dep} missing")
    top = {name: resolve(APP, name)
           for name in read(APP / "package.json")["dependencies"]}
    for name, source in top.items():
        visit(name, source, product)


def base():
    if STAGE.exists():
        raise RuntimeError("use a fresh Windows staging directory")
    copy_tree(PREPARED / "electron", STAGE)
    (STAGE / "electron.exe").rename(STAGE / "Maimi.exe")
    PRODUCT.mkdir()
    if read(PREPARED / "runtime/versions.json").get("pnpm")!="11.28.5":
        raise RuntimeError("Unrecognized primary runtime")
    if not (APP / "maimi/dws/manifest.json").is_file():
        raise RuntimeError("Stage pinned DingTalk assets first")
    if not (APP/"lib/welcome/montserrat-regular.woff2").is_file():
        raise RuntimeError("Prepare the OFL welcome fonts")

    shutil.copy2(APP / "package.json", PRODUCT)
    copy_tree(APP / "renderer", PRODUCT / "renderer")
    copy_tree(APP / "maimi", PRODUCT / "maimi")
    (PRODUCT / "lib").mkdir()
    for path in (APP / "lib").iterdir():
        if path.is_file() and path.suffix in (".cjs",".js") and path.name != "command-manager-entry.js":
            shutil.copy2(path, PRODUCT / "lib")
    copy_tree(APP / "lib/welcome", PRODUCT / "lib/welcome")
    shell_deps()
    copy_tree(PREPARED / "runtime", RESOURCE / "runtime", ("__pycache__",))
    icon = REPO / "assets/icon.png"
    if hashlib.sha256(icon.read_bytes()).hexdigest() != "17432e1a34777091a18608f0918e1f30e6aa6021c586f8fc5e50ead84b6c9de3":
        raise RuntimeError("original icon changed")
    shutil.copy2(icon, RESOURCE / "icon.png")
    shutil.copy2(REPO / "assets/icon.ico", RESOURCE / "tray.ico")
    (RESOURCE / "app-update.yml").write_text(
        f"provider: generic\nurl: {FEED}\nchannel: latest\nuseMultipleRangeRequest: false\nupdaterCacheDirName: maimi-desktop-updater\n", encoding="utf-8")
    # The updater requires this helper for an NSIS elevation handoff.
    shutil.copy2(PREPARED / "elevate.exe", RESOURCE)
    print("BASE_READY", flush=True)


def runtime():
    source = REPO / "runtime"
    if read(source / "node_modules/@deepseek-ai/dsh/package.json")["version"] != RELEASE:
        raise RuntimeError("wrong DSH release")
    dest = PRODUCT / "dsh"
    dest.mkdir(exist_ok=False)
    shutil.copy2(source / "package.json", dest)
    copy_tree(source / "node_modules", dest / "node_modules",
              (".bin", ".pnpm", ".modules.yaml", ".pnpm-workspace-state-v1.json", "dsh-any-background"), omit_debug_metadata=True)
    copy_tree(source / "plugins", dest / "plugins", (".bin", ".pnpm"))
    market = source / "node_modules/dshmarket"
    if read(market / "package.json")["version"] != read(source / "package.json")["dependencies"]["dshmarket"]:
        raise RuntimeError("Installed dshmarket does not match the desktop release")
    copy_tree(market, dest / "plugins/managed/runtime/market", ("node_modules",))
    subprocess.run(["node", str(SCRIPTS / "seal_runtime.mjs"), str(dest)], check=True)
    print("RUNTIME_READY", flush=True)


if __name__=="__main__":
    opts=argparse.ArgumentParser()
    opts.add_argument("step",choices=("base","runtime"))
    {"base":base,"runtime":runtime}[opts.parse_args().step]()
