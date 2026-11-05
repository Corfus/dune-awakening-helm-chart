#!/usr/bin/env python3
"""Create credentials once, keeping all plaintext in memory rather than Git or CLI args."""

import argparse
import base64
import getpass
import json
from pathlib import Path
import re
import secrets
import string
import subprocess
import sys
import uuid


def token_host_id(token):
    try:
        payload = token.split(".")[1]
        payload = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        host = str(json.loads(payload)["HostId"]).lower()
    except (ValueError, KeyError, IndexError):
        raise ValueError("The self-hosting token must contain a HostId claim") from None
    if not re.fullmatch(r"[a-z0-9-]{1,40}", host):
        raise ValueError("The self-hosting token has an invalid HostId")
    # This only extracts the identity; Funcom validates the token itself.
    return host


def backup_credentials(namespace, name):
    label = "igw.funcom.com/backup-credentials"
    result = subprocess.run(
        ["kubectl", "-n", namespace, "get", "secret", name, "--ignore-not-found", "-o", "json"],
        check=True, capture_output=True, text=True,
    )
    if result.stdout.strip():
        existing = json.loads(result.stdout)
        data = existing.get("data", {})
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            try:
                valid = bool(base64.b64decode(data.get(key, ""), validate=True))
            except ValueError:
                valid = False
            if not valid:
                raise RuntimeError(f"Existing {name} has missing or empty backup keys; repair it explicitly to avoid credential rotation")
        labels = dict(existing["metadata"].get("labels", {}))
        if labels.get(label) != name:
            patch = {"metadata": {
                "resourceVersion": existing["metadata"]["resourceVersion"],
                "labels": {label: name},
            }}
            updated = subprocess.run(
                ["kubectl", "-n", namespace, "patch", "secret", name,
                 "--type=merge", "--patch-file=/dev/stdin"],
                input=json.dumps(patch), capture_output=True, text=True,
            )
            if updated.returncode:
                raise RuntimeError("Labeling the backup Secret failed; retry after checking context and permissions")
        labels[label] = name
        secret = {
            "apiVersion": "v1", "kind": "Secret", "type": existing.get("type", "Opaque"),
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "data": data,
        }
        print(f"Reusing existing {name}; backup credentials are preserved.")
    else:
        secret = {
            "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": name, "namespace": namespace, "labels": {label: name}},
            "data": {key: base64.b64encode(secrets.token_urlsafe(32).encode()).decode()
                     for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")},
        }
        created = subprocess.run(
            ["kubectl", "create", "-f", "-"], input=json.dumps(secret),
            capture_output=True, text=True,
        )
        if created.returncode:
            raise RuntimeError("Creating the backup Secret failed; check the namespace, context and permissions")
        print(f"Created {name} with persistent backup credentials.")
    return secret


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="dune")
    parser.add_argument("--sealed-output", type=Path, help="Also encrypt both existing/new Secrets into one YAML file using kubeseal")
    parser.add_argument("--backup-secret-name", default="dune-backup-credentials",
                        help="Backup Secret name; must match backups.existingSecret")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", args.backup_secret_name) or len(args.backup_secret_name) > 63:
        raise ValueError("Backup Secret name must be a DNS label of at most 63 characters")
    if args.backup_secret_name == "dune-credentials":
        raise ValueError("Backup Secret must be separate from dune-credentials")
    result = subprocess.run(
        ["kubectl", "-n", args.namespace, "get", "secret", "dune-credentials", "--ignore-not-found", "-o", "json"],
        check=True, capture_output=True, text=True,
    )
    if result.stdout.strip():
        existing = json.loads(result.stdout)
        secret = {
            "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": "dune-credentials", "namespace": args.namespace},
            "data": existing["data"],
        }
        if "fls-apikey" not in secret["data"]:
            # Optimistic concurrency protects against a simultaneous credential update.
            key = base64.b64encode(str(uuid.uuid4()).encode()).decode()
            patch = {
                "metadata": {"resourceVersion": existing["metadata"]["resourceVersion"]},
                "data": {"fls-apikey": key},
            }
            updated = subprocess.run(
                ["kubectl", "-n", args.namespace, "patch", "secret", "dune-credentials",
                 "--type=merge", "--patch-file=/dev/stdin"],
                input=json.dumps(patch), capture_output=True, text=True,
            )
            if updated.returncode:
                raise RuntimeError("Adding the gateway key failed; retry after checking context and permissions")
            secret["data"]["fls-apikey"] = key
            print("Added a persistent gateway API key to existing dune-credentials.")
        print("Reusing existing dune-credentials; database credentials and world identity are preserved.")
    else:
        token = getpass.getpass("Funcom self-hosting token: ").strip()
        host = token_host_id(token)
        login_password = getpass.getpass("Player login password (empty for public access): ")
        if any(char in login_password for char in "\r\n\"'|"):
            raise ValueError("Player password cannot contain newlines, quotes, apostrophes or pipes")
        suffix = "".join(secrets.choice(string.ascii_lowercase) for _ in range(6))
        values = {
            "fls-token": token,
            "fls-apikey": str(uuid.uuid4()),
            "server-login-password": login_password,
            "world-id": f"sh-{host}-{suffix}",
            "database-password": secrets.token_urlsafe(32),
            "postgres-password": secrets.token_urlsafe(32),
            "rmq-auth-secret": base64.b64encode(secrets.token_bytes(64)).decode(),
        }
        secret = {
            "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": "dune-credentials", "namespace": args.namespace},
            "data": {key: base64.b64encode(value.encode()).decode() for key, value in values.items()},
        }
        # Capture output because validation errors can echo the submitted object.
        created = subprocess.run(
            ["kubectl", "create", "-f", "-"], input=json.dumps(secret),
            capture_output=True, text=True,
        )
        if created.returncode:
            raise RuntimeError("Creating dune-credentials failed; check the namespace, context and permissions")
        print("Created dune-credentials.")
    backup_secret = backup_credentials(args.namespace, args.backup_secret_name)
    if args.sealed_output:
        documents = []
        for item in (secret, backup_secret):
            sealed = subprocess.run(
                ["kubeseal", "--format", "yaml"], input=json.dumps(item),
                capture_output=True, text=True,
            )
            if sealed.returncode:
                raise RuntimeError("kubeseal failed; check its connection to the sealed-secrets controller")
            documents.append(sealed.stdout.rstrip())
        args.sealed_output.write_text("\n---\n".join(documents) + "\n")
        print(f"Both encrypted Secrets written to {args.sealed_output}; add that file to kustomization.yaml resources.")



if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        # Do not echo subprocess input/stderr containing credentials.
        if isinstance(error, subprocess.CalledProcessError):
            print("kubectl failed; check the context, namespace and permissions.", file=sys.stderr)
        else:
            print(str(error), file=sys.stderr)
        sys.exit(1)
