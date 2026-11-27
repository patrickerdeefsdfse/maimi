"""Render an additive, sales-service-only rule for approved Oracle listener IP addresses."""
import argparse
import ipaddress
import json

def main():
    args=argparse.ArgumentParser()
    args.add_argument("--ip",action="append",required=True,type=ipaddress.ip_address)
    args.add_argument("--port",required=True,type=int)
    chosen=args.parse_args()
    if not 1<=chosen.port<=65535:args.error("listener TCP port must be in 1..65535")
    if any(ip.is_unspecified or ip.is_loopback or ip.is_multicast for ip in chosen.ip):
        args.error("an approved unicast listener IP is required")
    peers=[{"ipBlock":{"cidr":f"{ip}/{ip.max_prefixlen}"}} for ip in dict.fromkeys(chosen.ip)]
    policy={"apiVersion":"networking.k8s.io/v1","kind":"NetworkPolicy",
            "metadata":{"name":"sales-orders-to-oracle","namespace":"maimi-production"},
            "spec":{"podSelector":{"matchLabels":{"service-2.example.invalid/component":"sales-orders"}},"policyTypes":["Egress"],
                    "egress":[{"to":peers,"ports":[{"protocol":"TCP","port":chosen.port}]}]}}
    print(json.dumps(policy,indent=2))

if __name__=="__main__":
    main()
