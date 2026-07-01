#!/usr/bin/env python3
"""
migrate-secrets-to-asm.py - copy the demo's runtime Kubernetes Secrets into AWS
Secrets Manager under the <CLUSTER_NAME>/* prefix, so External Secrets Operator
can sync them back into the cluster (ASM becomes the source of truth).

Idempotent: creates each ASM secret if missing, otherwise puts a new value.
Reads the live cluster secrets (the current source of truth) and writes the
exact same key/value map as a JSON ASM secret. Secret values are passed to the
AWS CLI via mode-600 temp files (file://), never on the command line.

Usage:
    . ~/.config/<CLUSTER_NAME>/env
    export KUBECONFIG=./kubeconfig
    python3 scripts/migrate-secrets-to-asm.py [--dry-run]
"""
import base64
import json
import os
import subprocess
import sys
import tempfile

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-gov-west-1")
DRY = "--dry-run" in sys.argv[1:]

# ASM secret name -> (namespace, kubernetes secret name)
MAPPING = {
    "<CLUSTER_NAME>/coder/db": ("coder", "coder-db"),
    "<CLUSTER_NAME>/coder/oidc": ("coder", "coder-oidc"),
    "<CLUSTER_NAME>/coder/ai": ("coder", "coder-ai"),
    "<CLUSTER_NAME>/coder/external-auth": ("coder", "coder-external-auth"),
    "<CLUSTER_NAME>/coder/provisioner-alpha": ("coder", "coder-provisioner-alpha"),
    "<CLUSTER_NAME>/coder/provisioner-bravo": ("coder", "coder-provisioner-bravo"),
    "<CLUSTER_NAME>/keycloak/admin": ("keycloak", "keycloak-admin"),
    "<CLUSTER_NAME>/keycloak/db": ("keycloak", "keycloak-db"),
    "<CLUSTER_NAME>/gitlab/secrets": ("gitlab", "gitlab-secrets"),
}


def sh(args, check=True, capture=True):
    return subprocess.run(args, check=check,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE)


def read_k8s_secret(ns, name):
    out = sh(["kubectl", "-n", ns, "get", "secret", name, "-o", "json"]).stdout
    data = json.loads(out).get("data", {})
    return {k: base64.b64decode(v).decode("utf-8") for k, v in data.items()}


def asm_exists(name):
    r = sh(["aws", "secretsmanager", "describe-secret", "--region", REGION,
            "--secret-id", name], check=False)
    return r.returncode == 0


def put_asm(name, payload):
    fd, path = tempfile.mkstemp(prefix="asm-", suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        ref = "file://" + path
        if asm_exists(name):
            sh(["aws", "secretsmanager", "put-secret-value", "--region", REGION,
                "--secret-id", name, "--secret-string", ref])
            return "updated"
        else:
            sh(["aws", "secretsmanager", "create-secret", "--region", REGION,
                "--name", name,
                "--description", "<CLUSTER_NAME> demo secret (synced to k8s by ESO)",
                "--secret-string", ref])
            return "created"
    finally:
        os.unlink(path)


def main():
    for asm_name, (ns, k8s_name) in MAPPING.items():
        payload = read_k8s_secret(ns, k8s_name)
        keys = ",".join(sorted(payload))
        if DRY:
            print(f"[dry-run] {asm_name} <- {ns}/{k8s_name} keys=[{keys}]")
            continue
        action = put_asm(asm_name, payload)
        print(f"{action:8} {asm_name} <- {ns}/{k8s_name} keys=[{keys}]")


if __name__ == "__main__":
    main()
