#!/usr/bin/env python3
"""Authorize worker access ports from the caller's current public IP."""

from __future__ import annotations

import argparse
import json
import subprocess
import urllib.request
from typing import Iterable


def detect_public_ip() -> str:
    with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10) as response:
        return response.read().decode("utf-8").strip()


def existing_ranges(group_id: str, region: str) -> list[dict[str, object]]:
    output = subprocess.check_output(
        [
            "aws",
            "ec2",
            "describe-security-groups",
            "--group-ids",
            group_id,
            "--region",
            region,
            "--query",
            "SecurityGroups[0].IpPermissions",
            "--output",
            "json",
        ],
        text=True,
    )
    return json.loads(output)


def has_rule(permissions: Iterable[dict[str, object]], *, port: int, cidr: str) -> bool:
    for permission in permissions:
        if permission.get("IpProtocol") != "tcp":
            continue
        if int(permission.get("FromPort") or -1) != port:
            continue
        if int(permission.get("ToPort") or -1) != port:
            continue
        for ip_range in permission.get("IpRanges") or []:
            if ip_range.get("CidrIp") == cidr:
                return True
    return False


def authorize_rule(
    *,
    group_id: str,
    region: str,
    port: int,
    cidr: str,
    description: str,
) -> None:
    subprocess.check_call(
        [
            "aws",
            "ec2",
            "authorize-security-group-ingress",
            "--group-id",
            group_id,
            "--region",
            region,
            "--ip-permissions",
            json.dumps(
                [
                    {
                        "IpProtocol": "tcp",
                        "FromPort": port,
                        "ToPort": port,
                        "IpRanges": [
                            {
                                "CidrIp": cidr,
                                "Description": description,
                            }
                        ],
                    }
                ]
            ),
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group-id", required=True)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--port", type=int, action="append", required=True)
    parser.add_argument(
        "--description",
        default="newscom worker access",
        help="Security-group rule description for any newly added ingress rule.",
    )
    args = parser.parse_args()

    ip = detect_public_ip()
    cidr = f"{ip}/32"
    permissions = existing_ranges(args.group_id, args.region)
    updated_ports: list[int] = []
    existing_ports: list[int] = []
    for port in args.port:
        if has_rule(permissions, port=port, cidr=cidr):
            existing_ports.append(port)
            continue
        authorize_rule(
            group_id=args.group_id,
            region=args.region,
            port=port,
            cidr=cidr,
            description=args.description,
        )
        updated_ports.append(port)

    print(
        json.dumps(
            {
                "group_id": args.group_id,
                "region": args.region,
                "public_ip": ip,
                "cidr": cidr,
                "added_ports": updated_ports,
                "existing_ports": existing_ports,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
