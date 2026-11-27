"""Load immutable portal assets and verify release digests at startup."""
from pathlib import Path
import hashlib
import json
import os

def load_assets():
    source = Path(__file__).with_name("cosmos")
    external = Path(os.getenv("MAIMI_PORTAL_ASSETS", str(source)))
    assets = {}
    for url, item in json.loads((source / "manifest.json").read_text("utf-8")).items():
        folder = external if item.get("requires_site_asset") else source
        name = item["name"]
        if Path(name).name != name:
            raise ValueError("Invalid portal asset name")
        raw = (folder / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("Invalid portal asset hash")
        assets[url] = (raw, item["type"])
    return assets
