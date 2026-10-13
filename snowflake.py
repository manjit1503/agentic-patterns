#!/usr/bin/env python3
"""
Snowflake <-> Microsoft Entra ID (Service Principal) OAuth debugger
--------------------------------------------------------------------
Reproduces exactly what the Copilot Studio / Power Platform Snowflake connector does:
  1. Gets a client_credentials token from Entra ID
  2. Calls the Snowflake SQL API v2 with that Bearer token
...and tells you which layer is failing (network, Entra, token claims, Snowflake auth,
role, or data access). Optional --admin mode logs in as YOU (SSO) and checks the
Snowflake-side config (security integration, user mapping, grants, login history).

Setup:
  pip install requests
  pip install snowflake-connector-python      # only needed for --admin

  # PowerShell:  $env:SF_TENANT_ID="..."   bash:  export SF_TENANT_ID=...
  SF_TENANT_ID      Kenvue Entra tenant ID (GUID)
  SF_CLIENT_ID      Client app (service principal) Application ID
  SF_CLIENT_SECRET  Client secret VALUE (prompted if not set)
  SF_RESOURCE       Resource app Application ID URI, e.g. api://snowflake-kenvue-prod

Run:
  python snowflake_oauth_debug.py
  python snowflake_oauth_debug.py --admin-user you@kenvue.com --admin-role SECURITYADMIN
"""
import argparse
import base64
import getpass
import ipaddress
import json
import os
import socket
import ssl
import sys
import time
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

DEFAULTS = dict(
    account="KENVUE-CY11773",
    role="PROD_VDARDEN1_ER",
    warehouse="PROD_ENVIRONMENT_USAGE_XSMALL1_WH",
    database="PROD_CONSUMER360_NONPII",
    schema="PAIDMEDIA_ACCESS",
)

# Common Entra errors when acquiring the token
AAD_HINTS = {
    "AADSTS7000215": "Invalid client secret. Usually the Secret ID was pasted instead of the Secret VALUE.",
    "AADSTS7000222": "Client secret has expired. Create a new one.",
    "AADSTS700016": "Client ID not found in this tenant. Check SF_CLIENT_ID / SF_TENANT_ID.",
    "AADSTS500011": "Resource not found. SF_RESOURCE must exactly match the Resource app's Application ID URI.",
    "AADSTS90002": "Tenant not found. Check SF_TENANT_ID.",
    "AADSTS70011": "Invalid scope. Resource URL must be the App ID URI (script appends /.default).",
    "AADSTS53003": "Blocked by a Conditional Access policy for workload identities.",
}

# Common Snowflake auth error codes (unknown codes are printed raw)
SF_HINTS = {
    "390303": "Invalid OAuth token: iss/aud/signature don't match the security integration, OR the "
              "mapping claim (sub) doesn't match any user's LOGIN_NAME.",
    "390318": "OAuth token expired.",
    "390186": "Requested role is not in the token's roles claim, or not granted to the mapped user.",
    "390422": "Blocked by a Snowflake network policy. Client IP not allowed.",
    "390100": "Username/password auth was attempted. The connection is not using OAuth.",
}

RESULTS = []


def section(title):
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def check(name, ok, detail="", hint=""):
    """ok: True=PASS, False=FAIL, None=WARN"""
    RESULTS.append((name, ok, detail, hint))
    tag = {True: "PASS", False: "FAIL", None: "WARN"}[ok]
    print(f"[{tag}] {name}" + (f"\n       -> {detail}" if detail else ""))
    if hint and ok is not True:
        print(f"       hint: {hint}")


def b64url_json(segment):
    segment += "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment))


# --------------------------------------------------------------------------- 1
def check_network(host):
    section(f"1. Network reachability -> {host}:443")
    try:
        ips = sorted({i[4][0] for i in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})
        check("DNS resolution", True, ", ".join(ips))
    except socket.gaierror as e:
        check("DNS resolution", False, str(e),
              "Account identifier wrong? Expected <org>-<account>.snowflakecomputing.com")
        return False

    private = [ip for ip in ips if ipaddress.ip_address(ip).is_private]
    if private:
        check("Endpoint is public", None, f"Resolves to private IP(s): {private}",
              "PrivateLink/private DNS in play. Power Platform cloud connectors can't reach private "
              "IPs unless Power Platform VNet support or a data gateway is configured.")

    pl_host = host.replace(".snowflakecomputing.com", ".privatelink.snowflakecomputing.com")
    try:
        pl_ips = sorted({i[4][0] for i in socket.getaddrinfo(pl_host, 443)})
        print(f"       info: PrivateLink host {pl_host} also resolves -> {pl_ips}")
    except socket.gaierror:
        pass

    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=10) as s:
            with ctx.wrap_socket(s, server_hostname=host) as ss:
                issuer = dict(x[0] for x in ss.getpeercert()["issuer"]).get("organizationName", "?")
                check("TCP + TLS handshake", True, f"{ss.version()}, cert issuer: {issuer}")
    except ssl.SSLCertVerificationError as e:
        check("TCP + TLS handshake", False, repr(e),
              "TLS inspection proxy is re-signing traffic. Expected for corp laptops, not for Power Platform.")
        return True  # still allow further steps
    except Exception as e:
        check("TCP + TLS handshake", False, repr(e), "Firewall/proxy blocking outbound 443.")
        return False
    return True


# --------------------------------------------------------------------------- 2
def get_token(tenant, client_id, secret, resource):
    section("2. Entra ID token (client_credentials, same as the connector)")
    scope = resource.rstrip("/")
    if scope.endswith("/.default"):
        print("       note: Resource URL should NOT include /.default in the Copilot connection field.")
    else:
        scope += "/.default"
    print(f"       scope: {scope}")

    r = requests.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={"client_id": client_id, "client_secret": secret,
              "grant_type": "client_credentials", "scope": scope},
        timeout=30,
    )
    try:
        body = r.json()
    except ValueError:
        body = {"error_description": r.text[:500]}

    if r.status_code != 200:
        desc = body.get("error_description", "")
        code = desc.split(":")[0] if desc.startswith("AADSTS") else ""
        check("Token acquired", False, f"HTTP {r.status_code}: {desc.splitlines()[0] if desc else body}",
              AAD_HINTS.get(code, "See AADSTS code above."))
        return None

    check("Token acquired", True, f"expires_in={body.get('expires_in')}s")
    return body["access_token"]


# --------------------------------------------------------------------------- 3
def inspect_token(token, tenant, resource, role):
    section("3. Token claims (what the Snowflake security integration validates)")
    header, payload = (b64url_json(p) for p in token.split(".")[:2])

    for k in ["ver", "iss", "aud", "sub", "oid", "appid", "azp", "tid", "idtyp",
              "roles", "scp", "upn", "email", "exp"]:
        if k in payload:
            v = payload[k]
            if k == "exp":
                v = f"{v} ({datetime.fromtimestamp(v, timezone.utc).isoformat()})"
            print(f"   {k:6}: {v}")

    ver, iss, aud = payload.get("ver"), payload.get("iss"), payload.get("aud")
    sub = payload.get("sub")

    # Issuer vs version
    expected_iss = (f"https://sts.windows.net/{tenant}/" if ver == "1.0"
                    else f"https://login.microsoftonline.com/{tenant}/v2.0")
    check("Issuer matches token version", iss == expected_iss, f"ver={ver}, iss={iss}",
          f"Unexpected issuer. Whatever it is, EXTERNAL_OAUTH_ISSUER must equal it exactly.")
    print(f"       -> EXTERNAL_OAUTH_ISSUER must be exactly: '{iss}'  (trailing slash matters)")

    # Audience
    aud_ok = aud == resource.rstrip("/").replace("/.default", "")
    check("Audience", True if aud_ok else None, f"aud={aud}",
          "v2 tokens carry the Resource app CLIENT ID (GUID), not the App ID URI. "
          "Put this exact value in EXTERNAL_OAUTH_AUDIENCE_LIST.")
    print(f"       -> EXTERNAL_OAUTH_AUDIENCE_LIST must include: '{aud}'")

    # App-only vs delegated
    if "scp" in payload:
        check("Token type is app-only", None, f"scp={payload['scp']}",
              "This is a DELEGATED token, not a service principal token.")
    else:
        check("Token type is app-only", True, "no scp claim (client_credentials)")

    if "upn" not in payload and "email" not in payload:
        check("User mapping claim", None,
              "No upn/email in token. Snowflake can ONLY map via 'sub'.",
              f"Integration: EXTERNAL_OAUTH_TOKEN_USER_MAPPING_CLAIM='sub', mapping attribute "
              f"LOGIN_NAME, and a Snowflake user with LOGIN_NAME='{sub}'. "
              f"Mapping to VDARDEN1@KENVUE.COM will never work with a service principal.")

    # Role
    roles = payload.get("roles", [])
    wanted = f"session:role:{role}"
    if wanted in roles or "session:role-any" in roles:
        check("Role scope in token", True, f"roles={roles}")
    elif any(r.lower() == wanted.lower() for r in roles):
        check("Role scope in token", False, f"roles={roles}",
              f"Case mismatch. App role value must be exactly '{wanted}' (uppercase role name).")
    elif not roles:
        check("Role scope in token", False, "roles claim is empty/missing",
              "On the Client app: API permissions -> Resource app -> Application permission "
              f"'{wanted}' -> Grant admin consent.")
    else:
        check("Role scope in token", False, f"roles={roles}",
              f"Expected '{wanted}'. Add/assign that app role and grant admin consent.")

    # Expiry
    exp = payload.get("exp", 0)
    check("Token not expired", exp > time.time(), f"{int(exp - time.time())}s remaining")

    # Signing key published at JWKS URL Snowflake will use
    try:
        jwks_url = f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys"
        kids = {k["kid"] for k in requests.get(jwks_url, timeout=15).json()["keys"]}
        check("Signing key (kid) found at JWKS URL", header.get("kid") in kids,
              f"kid={header.get('kid')}  url={jwks_url}",
              "Token is signed with an app-specific key. Use JWKS URL with ?appid=<resource app id>.")
    except Exception as e:
        check("Signing key (kid) found at JWKS URL", None, repr(e))

    return payload


# --------------------------------------------------------------------------- 4
def sql_api(host, token, statement, label, **ctx):
    url = f"https://{host}/api/v2/statements"
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Snowflake-Authorization-Token-Type": "OAUTH",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "sf-oauth-debug/1.0",
    }
    body = {"statement": statement, "timeout": 60, **{k: v for k, v in ctx.items() if v}}
    print(f"\n   -> {label}\n      context: { {k: v for k, v in ctx.items() if v} or '(none)'}")

    try:
        r = requests.post(url, headers=headers, json=body, timeout=90)
        for _ in range(30):  # poll async execution
            if r.status_code != 202:
                break
            time.sleep(2)
            r = requests.get(f"https://{host}{r.json()['statementStatusUrl']}", headers=headers, timeout=60)
    except Exception as e:
        check(label, False, repr(e))
        return None

    try:
        j = r.json()
    except ValueError:
        j = {"message": r.text[:500]}

    if r.status_code == 200:
        check(label, True, json.dumps(j.get("data"))[:400])
        return j

    code = str(j.get("code", ""))
    msg = j.get("message", "")
    hint = SF_HINTS.get(code, "")
    if not hint and "network policy" in msg.lower():
        hint = SF_HINTS["390422"]
    check(label, False, f"HTTP {r.status_code}  code={code}  {msg}",
          hint or "Run with --admin-user to see the server-side reason in LOGIN_HISTORY.")
    return None


def test_snowflake(host, token, cfg):
    section("4. Snowflake SQL API (same endpoint the connector uses)")
    ident = "SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_ACCOUNT()"

    # 4a: auth only, no role/warehouse, isolates OAuth mapping from role problems
    auth_ok = sql_api(host, token, ident, "4a. Authenticate (no role/warehouse)")
    # 4b: with explicit role
    role_ok = sql_api(host, token, ident, "4b. Authenticate + role", role=cfg.role)
    # 4c: full context + data access
    data_ok = sql_api(
        host, token,
        f"SELECT TABLE_NAME, TABLE_TYPE FROM {cfg.database}.INFORMATION_SCHEMA.TABLES "
        f"WHERE TABLE_SCHEMA = '{cfg.schema}' ORDER BY TABLE_NAME LIMIT 20",
        "4c. Warehouse + DB/schema visibility",
        role=cfg.role, warehouse=cfg.warehouse, database=cfg.database, schema=cfg.schema,
    )

    if auth_ok and not role_ok:
        print("\n   => Auth works; ROLE is the problem (token roles claim or GRANT ROLE to user).")
    if role_ok and not data_ok:
        print("\n   => Auth + role work; missing USAGE on warehouse/db/schema or SELECT grants.")
    if role_ok and data_ok and not data_ok.get("data"):
        print("\n   => Connected, but role sees zero objects in the schema. Check SELECT/USAGE grants.")


# --------------------------------------------------------------------------- 5
def admin_checks(cfg, admin_user, admin_role, token, payload):
    section(f"5. Server-side checks as {admin_user} ({admin_role}) via SSO")
    try:
        import snowflake.connector
    except ImportError:
        print("   pip install snowflake-connector-python  to enable admin checks")
        return

    conn = snowflake.connector.connect(
        account=cfg.account, user=admin_user, authenticator="externalbrowser",
        role=admin_role, warehouse=cfg.warehouse, database=cfg.database,
    )
    cur = conn.cursor()

    def run(sql, params=None):
        try:
            cur.execute(sql, params)
            cols = [c[0] for c in cur.description] if cur.description else []
            return cols, cur.fetchall()
        except Exception as e:
            print(f"   ! {sql.split()[0]} ... failed: {e}")
            return [], []

    # Snowflake's own verdict on the token
    if token:
        _, rows = run("SELECT SYSTEM$VERIFY_EXTERNAL_OAUTH_TOKEN(%s)", (token,))
        if rows:
            print(f"\n   SYSTEM$VERIFY_EXTERNAL_OAUTH_TOKEN:\n   {rows[0][0]}")

    # Security integrations vs token
    _, ints = run("SHOW SECURITY INTEGRATIONS")
    oauth_ints = [r[0] for r in ints if "EXTERNAL_OAUTH" in str(r[1]).upper()]
    if not oauth_ints:
        check("External OAuth integration exists", False, "none found",
              "Create a SECURITY INTEGRATION of TYPE = EXTERNAL_OAUTH, EXTERNAL_OAUTH_TYPE = AZURE.")
    for name in oauth_ints:
        _, props = run(f'DESC SECURITY INTEGRATION "{name}"')
        p = {r[0]: str(r[2]) for r in props}
        print(f"\n   Integration {name}:")
        for k in ["ENABLED", "EXTERNAL_OAUTH_ISSUER", "EXTERNAL_OAUTH_JWS_KEYS_URL",
                  "EXTERNAL_OAUTH_AUDIENCE_LIST", "EXTERNAL_OAUTH_TOKEN_USER_MAPPING_CLAIM",
                  "EXTERNAL_OAUTH_SNOWFLAKE_USER_MAPPING_ATTRIBUTE", "EXTERNAL_OAUTH_ANY_ROLE_MODE",
                  "EXTERNAL_OAUTH_BLOCKED_ROLES_LIST", "EXTERNAL_OAUTH_ALLOWED_ROLES_LIST"]:
            print(f"     {k:50} {p.get(k, '-')}")
        if payload:
            check(f"[{name}] enabled", p.get("ENABLED", "").lower() == "true")
            check(f"[{name}] issuer == token iss", p.get("EXTERNAL_OAUTH_ISSUER") == payload.get("iss"),
                  f"{p.get('EXTERNAL_OAUTH_ISSUER')}  vs  {payload.get('iss')}")
            check(f"[{name}] audience list contains token aud",
                  str(payload.get("aud")) in p.get("EXTERNAL_OAUTH_AUDIENCE_LIST", ""),
                  p.get("EXTERNAL_OAUTH_AUDIENCE_LIST"))
            check(f"[{name}] mapping claim is 'sub'",
                  "sub" in p.get("EXTERNAL_OAUTH_TOKEN_USER_MAPPING_CLAIM", "").lower(),
                  p.get("EXTERNAL_OAUTH_TOKEN_USER_MAPPING_CLAIM"))

    # Does a user exist whose LOGIN_NAME == token sub?
    if payload:
        sub = payload.get("sub", "")
        run("SHOW USERS")
        cols, users = run(
            'SELECT "name","login_name","disabled","default_role","type" '
            'FROM TABLE(RESULT_SCAN(LAST_QUERY_ID())) WHERE UPPER("login_name") = UPPER(%s)', (sub,))
        check("Snowflake user mapped to token sub", bool(users),
              str(users[0]) if users else f"no user with LOGIN_NAME='{sub}'",
              f"CREATE USER SVC_COPILOT_PAIDMEDIA TYPE=SERVICE LOGIN_NAME='{sub}' "
              f"DEFAULT_ROLE={cfg.role}; GRANT ROLE {cfg.role} TO USER SVC_COPILOT_PAIDMEDIA;")
        if users:
            _, grants = run(f'SHOW GRANTS TO USER "{users[0][0]}"')
            granted = [g[1] for g in grants]
            check(f"Role {cfg.role} granted to mapped user", cfg.role in granted, f"roles: {granted}")

    # Role privileges
    _, g = run(f'SHOW GRANTS TO ROLE "{cfg.role}"')
    relevant = [f"{r[1]} on {r[2]} {r[3]}" for r in g
                if cfg.warehouse in str(r[3]) or cfg.database in str(r[3])]
    print(f"\n   Grants on {cfg.role} touching WH/DB ({len(relevant)}):")
    for line in relevant[:25]:
        print(f"     {line}")

    # Network policy
    _, np = run("SHOW PARAMETERS LIKE 'NETWORK_POLICY' IN ACCOUNT")
    if np and np[0][1]:
        check("Account network policy", None, f"active: {np[0][1]}",
              "Power Platform outbound IPs for your region must be allowed, or a "
              "user-level network policy applied to the service user.")

    # Recent failed logins: the real reason Snowflake rejected the connector
    _, fails = run("""
        SELECT EVENT_TIMESTAMP, USER_NAME, CLIENT_IP, REPORTED_CLIENT_TYPE,
               FIRST_AUTHENTICATION_FACTOR, ERROR_CODE, ERROR_MESSAGE
        FROM TABLE(INFORMATION_SCHEMA.LOGIN_HISTORY(
               TIME_RANGE_START => DATEADD('hour', -3, CURRENT_TIMESTAMP()), RESULT_LIMIT => 1000))
        WHERE IS_SUCCESS = 'NO' AND FIRST_AUTHENTICATION_FACTOR ILIKE '%OAUTH%'
        ORDER BY EVENT_TIMESTAMP DESC LIMIT 15""")
    print(f"\n   Failed OAuth logins (last 3h): {len(fails)}")
    for f in fails:
        print(f"     {f[0]} | user={f[1]} | ip={f[2]} | {f[5]} {f[6]}")
    if not fails:
        print("     none. If the connector still fails, requests may not be reaching "
              "Snowflake at all (network policy/PrivateLink) or the user can't be identified.")
    conn.close()


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Debug Snowflake External OAuth (Entra SP) for Copilot Studio")
    for k, v in DEFAULTS.items():
        ap.add_argument(f"--{k}", default=os.getenv(f"SF_{k.upper()}", v))
    ap.add_argument("--tenant", default=os.getenv("SF_TENANT_ID"))
    ap.add_argument("--client-id", default=os.getenv("SF_CLIENT_ID"))
    ap.add_argument("--resource", default=os.getenv("SF_RESOURCE"),
                    help="Resource app Application ID URI, e.g. api://snowflake-kenvue-prod")
    ap.add_argument("--admin-user", help="Your Snowflake login for SSO-based server-side checks")
    ap.add_argument("--admin-role", default="SECURITYADMIN")
    ap.add_argument("--skip-network", action="store_true")
    cfg = ap.parse_args()

    missing = [n for n in ("tenant", "client_id", "resource") if not getattr(cfg, n)]
    if missing:
        sys.exit(f"Missing: {missing}. Set SF_TENANT_ID / SF_CLIENT_ID / SF_RESOURCE or pass flags.")
    secret = os.getenv("SF_CLIENT_SECRET") or getpass.getpass("Client secret VALUE: ")

    host = f"{cfg.account.lower().replace('_', '-')}.snowflakecomputing.com"
    print(f"Account host: {host}\nRole: {cfg.role} | WH: {cfg.warehouse} | "
          f"DB.SCHEMA: {cfg.database}.{cfg.schema}")

    net_ok = True if cfg.skip_network else check_network(host)
    token = get_token(cfg.tenant, cfg.client_id, secret, cfg.resource)
    payload = inspect_token(token, cfg.tenant, cfg.resource, cfg.role) if token else None
    if token and net_ok:
        test_snowflake(host, token, cfg)
    if cfg.admin_user:
        admin_checks(cfg, cfg.admin_user, cfg.admin_role, token, payload)

    section("SUMMARY")
    for name, ok, detail, hint in RESULTS:
        if ok is not True:
            print(f"[{'FAIL' if ok is False else 'WARN'}] {name}\n       {hint or detail}")
    if all(r[1] is True for r in RESULTS):
        print("All checks passed. If Copilot Studio still fails, compare its connection fields "
              "(instance, Resource URL, secret value) with what this script used.")


if __name__ == "__main__":
    main()