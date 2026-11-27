"""Prepare desktop sources for a specific deployment in a new local directory."""
import argparse
from pathlib import Path
import re
import shutil
from urllib.parse import urlsplit

GATEWAY = "desktop-gateway.example.invalid"
WEB = "desktop-web.example.invalid"

def host(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or not re.fullmatch(r"[a-zA-Z0-9.-]+(?::[0-9]{1,5})?", parsed.netloc)):
        raise argparse.ArgumentTypeError("Use an HTTPS origin without a path or credentials.")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise argparse.ArgumentTypeError("The port is outside 1–65535.")
    return parsed.netloc.lower()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--gateway", required=True, type=host)
    parser.add_argument("--web", required=True, type=host)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    target = args.destination.resolve()
    if target == source or target.is_relative_to(source) or target.exists():
        parser.error("Choose a new directory outside the source tree.")
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("node_modules", "__pycache__", ".git"))
    for file in target.rglob("*"):
        if file.is_file() and file.name != Path(__file__).name and file.suffix in {".py", ".ts", ".js", ".cjs", ".mjs", ".json"}:
            before = file.read_bytes()
            after = before.replace(GATEWAY.encode(), args.gateway.encode()).replace(WEB.encode(), args.web.encode())
            if before != after:
                file.write_bytes(after)

if __name__ == "__main__":
    main()
