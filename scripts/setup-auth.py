#!/usr/bin/env python3
"""Configure Pocket ID for cove, through its admin API (static API key in secrets/pocketid_api_key.txt).

  scripts/setup-auth.py                   create or update the `cove` OIDC client and the `cove` group,
                                          restrict the client to that group, write its secret to
                                          secrets/oidc_client_secret.txt (then restart web)
  scripts/setup-auth.py add-user NAME EMAIL [--admin]
                                          create a user in the `cove` group and print a sign-up link
  scripts/setup-auth.py login-link NAME   a one-time link (15 minutes) to sign in and add a passkey:
                                          first-time setup, or recovery after losing all passkeys

Reads COVE_PUBLIC_URL and COVE_AUTH_URL from .env. Talks to Pocket ID on 127.0.0.1:1411.
Links are printed to your terminal only; treat them like a password until they expire.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "http://127.0.0.1:1411/api"
CLIENT_ID, GROUP = "cove", "cove"


def env() -> dict:
    out = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def call(method: str, path: str, body: dict | None = None):
    key = (ROOT / "secrets" / "pocketid_api_key.txt").read_text().strip()
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-API-Key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        sys.exit(f"Pocket ID {method} {path} failed: {e.code} {e.read().decode()[:300]}")


def group_id() -> str:
    for g in (call("GET", f"/user-groups?search={GROUP}") or {}).get("data", []):
        if g["name"] == GROUP:
            return g["id"]
    return call("POST", "/user-groups", {"name": GROUP, "friendlyName": "cove users"})["id"]


def setup() -> None:
    e = env()
    public, auth = e.get("COVE_PUBLIC_URL", "").rstrip("/"), e.get("COVE_AUTH_URL", "").rstrip("/")
    if not public.startswith("http") or not auth.startswith("http"):
        sys.exit("set COVE_PUBLIC_URL and COVE_AUTH_URL in .env first (see docs/auth.md)")
    client = {
        "name": "cove", "description": "cove personal databases",
        "callbackURLs": [f"{public}/auth/callback"], "logoutCallbackURLs": [f"{public}/login?signed_out=1"],
        "launchURL": public + "/", "isPublic": False, "pkceEnabled": True, "skipConsent": True,
        "requiresReauthentication": False, "isGroupRestricted": True, "credentials": {"secrets": []},
    }
    if call("GET", f"/oidc/clients/{CLIENT_ID}") is None:
        call("POST", "/oidc/clients", {**client, "id": CLIENT_ID})
        print(f"created OIDC client '{CLIENT_ID}' → {public}/auth/callback")
    else:
        call("PUT", f"/oidc/clients/{CLIENT_ID}", client)
        print(f"updated OIDC client '{CLIENT_ID}' → {public}/auth/callback")
    gid = group_id()
    call("PUT", f"/oidc/clients/{CLIENT_ID}/allowed-user-groups", {"userGroupIds": [gid]})
    print(f"only members of the '{GROUP}' group can sign in to cove")
    secret_file = ROOT / "secrets" / "oidc_client_secret.txt"
    if not secret_file.read_text().strip():
        secret = call("POST", f"/oidc/clients/{CLIENT_ID}/secrets", {})["secret"]
        secret_file.write_text(secret)
        print("wrote secrets/oidc_client_secret.txt; now run: docker compose up -d web")
    else:
        print("client secret already present (delete secrets/oidc_client_secret.txt's contents to rotate)")


def find_user(name: str) -> dict | None:
    for u in (call("GET", f"/users?search={name}") or {}).get("data", []):
        if u["username"] == name:
            return u
    return None


def login_link(name: str) -> None:
    user = find_user(name) or sys.exit(f"no user '{name}'")
    token = call("POST", f"/users/{user['id']}/one-time-access-token", {"ttl": 900})["token"]
    print(f"one-time link for {name} (valid 15 minutes, works once):\n  {env()['COVE_AUTH_URL'].rstrip('/')}/lc/{token}")
    print("Open it, then add a passkey (Account → Passkeys). Add a second one on another device.")


def add_user(name: str, email: str, admin: bool) -> None:
    if find_user(name):
        sys.exit(f"user '{name}' already exists; use login-link")
    call("POST", "/users", {"username": name, "email": email, "emailVerified": True, "displayName": name,
                            "firstName": name, "isAdmin": admin, "userGroupIds": [group_id()]})
    print(f"created {'admin ' if admin else ''}user '{name}' in the '{GROUP}' group")
    login_link(name)


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        setup()
    elif args[0] == "login-link" and len(args) == 2:
        login_link(args[1])
    elif args[0] == "add-user" and len(args) in (3, 4) and "@" in args[2]:
        add_user(args[1], args[2], "--admin" in args[3:])
    else:
        sys.exit(__doc__)
