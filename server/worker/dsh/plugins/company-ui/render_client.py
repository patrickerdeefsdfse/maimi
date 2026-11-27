"""Produce the release JS from editable source and authorized image files.

For ordinary changes build in a staging directory, review the newly generated
SHA-256 and update the gateway asset pin. --verify-shipped checks exact release
fidelity, including the original display-only host example.
"""
import argparse
import base64
import hashlib
import ipaddress
import json
import re
from pathlib import Path


def installed_sample_host(installed):
    # Extract only the IPv4 placeholder in the cluster connection form.
    # Brand assets are validated individually; --verify-shipped pins the full result.
    before = "input('host','IP 地址',{placeholder:'例如 ".encode()
    after = b"',inputMode:'decimal'})"
    hosts = re.findall(re.escape(before) + rb"([0-9.]+)" + re.escape(after), installed)
    if len(hosts) != 1:
        raise ValueError("Installed company UI lacks the unique display-only host placeholder")
    return str(ipaddress.IPv4Address(hosts[0].decode("ascii")))


def main():
    parser=argparse.ArgumentParser()
    sources=parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--assets",type=Path)
    sources.add_argument("--installed-client",type=Path,help="Reuse digest-matched brand assets and the validated display-only host example")
    parser.add_argument("--sample-host",help="Override the display-only IPv4 example in an independent build")
    parser.add_argument("--verify-shipped",action="store_true")
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    src=Path(__file__).resolve().parent
    spec=json.loads((src/"client-assets.json").read_text("utf-8"))
    script=(src/"client.template.js").read_bytes()
    old=args.installed_client.read_bytes() if args.installed_client else None
    for name,pin in spec["assets"].items():
        if old is None:
            body=(args.assets/name).read_bytes()
        else:
            mime={"wordmark.svg":"svg+xml","software-icon.png":"png"}[name]
            marker=("data:image/"+mime+";base64,").encode()
            candidates=(base64.b64decode(v,validate=True) for v in
                        re.findall(re.escape(marker)+rb"([A-Za-z0-9+/=]+)",old))
            body=next((v for v in candidates if hashlib.sha256(v).hexdigest()==pin["sha256"]),b"")
        if hashlib.sha256(body).hexdigest()!=pin["sha256"] or len(body)!=pin["bytes"]:
            raise ValueError("Company UI asset mismatch: "+name)
        marker=("{{MAIMI_ASSET:"+name+"}}").encode()
        if script.count(marker)!=1: raise ValueError("Unexpected template marker")
        script=script.replace(marker,base64.b64encode(body))
    sample_host = args.sample_host
    if sample_host is None:
        sample_host = installed_sample_host(old) if old is not None else "192.0.2.14"
    if script.count(b"{{MAIMI_SAMPLE_HOST}}") != 1:
        raise ValueError("Unexpected host template marker")
    script=script.replace(b"{{MAIMI_SAMPLE_HOST}}",str(ipaddress.IPv4Address(sample_host)).encode())
    pin=hashlib.sha256(script).hexdigest()
    if args.verify_shipped and pin!=spec["release_sha256"]: raise ValueError("Shipped JS digest mismatch")
    if not args.output.exists() or args.output.read_bytes() != script:
        with args.output.open("xb") as out:out.write(script)
    print(pin)
if __name__=="__main__":main()
