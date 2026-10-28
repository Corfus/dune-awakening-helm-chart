#!/usr/bin/env python3
"""Render the official BattleGroup with mounted credentials and apply it in-cluster."""

import base64
import ipaddress
import json
import os
from pathlib import Path
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def render(template, credentials, namespace, node_ip):
    ipaddress.ip_address(node_ip)
    replacements = {
        "__DUNE_FLS_TOKEN__": credentials["fls-token"],
        "__DUNE_DATABASE_PASSWORD__": credentials["database-password"],
        "__DUNE_POSTGRES_PASSWORD__": credentials["postgres-password"],
        "__DUNE_NODE_IP__": node_ip,
    }

    def replace(value):
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, str):
            for key, replacement in replacements.items():
                value = value.replace(key, replacement)
        return value

    world = replace(template)
    world["metadata"]["name"] = credentials["world-id"]
    world["metadata"]["namespace"] = namespace
    world["spec"]["name"] = credentials["world-id"]
    # This build needs the login password at launch to advertise password protection
    # to Director/FLS; the mounted UserEngine.ini alone is insufficient.
    server_group = world["spec"]["serverGroup"]["template"]["spec"]
    password = credentials.get("server-login-password", "")
    if any(char in password for char in "\r\n\"'|"):
        raise ValueError("server-login-password cannot contain newlines, quotes, apostrophes or pipes")
    files = server_group.setdefault("global", {}).setdefault("userIniConfig", {}).setdefault("files", {})
    files["UserEngine.ini"] = files.get("UserEngine.ini", "").replace(
        'Bgd.ServerLoginPassword=""', 'Bgd.ServerLoginPassword="' + password + '"'
    )
    if password:
        server_group["global"].setdefault("additionalArguments", []).append(
            "-ini:engine:[ConsoleVariables]:Bgd.ServerLoginPassword=" + password
        )
    labels = {
        "igw.funcom.com/env-for-" + service: "default"
        for service in (
            "server", "server-gateway", "battlegroup-director", "message-queue", "text-router"
        )
    }
    gateway = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": "server-gateway-secret", "namespace": namespace, "labels": labels},
        "type": "Opaque",
        "stringData": {
            "fls-apikey": credentials["fls-apikey"],
            "FuncomLiveServices__ServiceAuthToken": credentials["fls-token"],
        },
    }
    rmq = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": "rmq-game-secret", "namespace": namespace, "labels": labels},
        "type": "Opaque",
        "stringData": {"RMQ_HTTP_TOKEN_AUTH_SECRET": credentials["rmq-auth-secret"]},
    }
    for secret in (gateway, rmq):
        secret["data"] = {
            key: base64.b64encode(value.encode()).decode()
            for key, value in secret.pop("stringData").items()
        }
    return gateway, rmq, world


def main():
    namespace = os.environ["POD_NAMESPACE"]
    node_ip = os.environ["NODE_IP"]
    credential_dir = Path("/credentials")
    keys = ("fls-token", "database-password", "postgres-password", "rmq-auth-secret", "world-id", "fls-apikey")
    credentials = {key: (credential_dir / key).read_text().strip() for key in keys}
    if not all(credentials.values()):
        raise ValueError("dune-credentials contains an empty value")
    login_file = credential_dir / "server-login-password"
    credentials["server-login-password"] = login_file.read_text() if login_file.exists() else ""
    template = json.loads(Path("/work/battlegroup.json").read_text())
    objects = render(template, credentials, namespace, node_ip)

    serviceaccount = Path("/var/run/secrets/kubernetes.io/serviceaccount")
    token = (serviceaccount / "token").read_text().strip()
    context = ssl.create_default_context(cafile=str(serviceaccount / "ca.crt"))
    # The standard in-cluster DNS name also works when the API's service IP is IPv6.
    server = "https://kubernetes.default.svc"
    quoted_ns = urllib.parse.quote(namespace, safe="")
    for obj in objects:
        kind = obj["kind"]
        name = obj["metadata"]["name"]
        if kind == "Secret":
            base = f"/api/v1/namespaces/{quoted_ns}/secrets"
        else:
            base = f"/apis/igw.funcom.com/v1/namespaces/{quoted_ns}/battlegroups"
        path = base + "/" + urllib.parse.quote(name, safe="")
        request = urllib.request.Request(
            server + path + "?fieldManager=dune-argocd-bootstrap",
            data=json.dumps(obj).encode(), method="PATCH",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/apply-patch+yaml"},
        )
        # Fresh Helm installs may reach this hook while cert-manager is still
        # issuing certificates or the admission webhook is starting.
        deadline = time.monotonic() + 480
        while True:
            try:
                with urllib.request.urlopen(request, context=context, timeout=30) as response:
                    response.read()
                break
            except urllib.error.HTTPError as error:
                # API validation responses may echo credentials contained in the CR.
                retry = error.code in (404, 409, 429, 500, 502, 503, 504)
                if not retry or time.monotonic() >= deadline:
                    raise RuntimeError(f"Applying {kind}/{name} failed: HTTP {error.code}; inspect operator logs") from None
            except (urllib.error.URLError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Applying {kind}/{name} failed: API connection timeout") from None
            print(f"Waiting for API/admission readiness for {kind}/{name}", flush=True)
            time.sleep(5)
        print(f"Applied {kind}/{name}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Do not print tracebacks, object bodies, or a repr of the rendered resource.
        print(f"BattleGroup configuration failed: {error}", file=sys.stderr)
        sys.exit(1)
