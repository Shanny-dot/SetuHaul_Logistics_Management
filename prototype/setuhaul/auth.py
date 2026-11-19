"""Who is calling: drivers by their WhatsApp number, coordinators by their sign-in.

Core activity: prove identity at the edge, before any business code runs. Identity
always comes from the channel, never from the message text or a model.

Drivers
    Meta signs every webhook body with the app secret (header X-Hub-Signature-256).
    verify_whatsapp_signature() checks it; the sender's number is then the user id and
    is looked up in drivers.phone (driver_for_phone).

Coordinators
    Production: Amazon Cognito sign-in (email or mobile) returns a JWT; verify_cognito()
    checks it against the user pool's public keys (needs PyJWT).
    Offline demo: issue_dev_token()/verify_dev_token() make an HS256 token with the
    standard library, so the console works without AWS.
    Either way the token id (jti) is registered in the session store, so logout and
    expiry are enforced on every call.
"""
import base64
import hashlib
import hmac
import json
import os
import time
import uuid

from .db import one
from .session_store import normalise_phone

COORD_ROLES = {"WAREHOUSE_COORDINATOR", "SHIFT_MANAGER", "NIGHT_COORDINATOR", "GATE_SUPERVISOR"}
TOKEN_TTL_S = 8 * 3600            # one shift


class AuthError(Exception):
    pass


# ---------------------------------------------------------------- drivers
def verify_whatsapp_signature(body: bytes, header: str, app_secret: str) -> bool:
    if not header or not app_secret:
        return False
    want = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header)


def driver_for_phone(con, phone):
    """Match any written form of the number to drivers.phone ('+91-9000010001')."""
    want = normalise_phone(phone)
    for r in con.execute("SELECT driver_id, phone, driver_status FROM drivers"):
        if normalise_phone(r["phone"]) == want:
            return dict(r)
    return None


# ---------------------------------------------------------------- coordinators
def coordinator_for(con, user_id):
    """A coordinator's user id is their email or their mobile number."""
    r = one(con, "SELECT * FROM facility_contacts WHERE active_flag=1 AND lower(email)=lower(?)", user_id)
    if not r:
        want = normalise_phone(user_id) if any(c.isdigit() for c in user_id) else None
        for c in con.execute("SELECT * FROM facility_contacts WHERE active_flag=1"):
            if want and c["phone"] and normalise_phone(c["phone"]) == want:
                r = c
                break
    if not r or r["contact_role"] not in COORD_ROLES:
        return None
    return {"user_id": user_id, "contact_id": r["contact_id"], "name": r["contact_name"],
            "role": r["contact_role"], "facility_id": r["facility_id"]}


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_dev_token(claims, secret, ttl_s=TOKEN_TTL_S, now=None):
    now = int(now or time.time())
    body = dict(claims, iat=now, exp=now + ttl_s, jti=uuid.uuid4().hex, iss="setuhaul-dev")
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps(body, separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret.encode(), f"{head}.{payload}".encode(), hashlib.sha256).digest())
    return f"{head}.{payload}.{sig}", body


def verify_dev_token(token, secret, now=None):
    try:
        head, payload, sig = token.split(".")
    except (AttributeError, ValueError):
        raise AuthError("malformed token")
    want = _b64(hmac.new(secret.encode(), f"{head}.{payload}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(want, sig):
        raise AuthError("bad signature")
    claims = json.loads(_unb64(payload))
    if claims.get("exp", 0) <= (now or time.time()):
        raise AuthError("expired")
    return claims


def verify_cognito(token, region=None, pool_id=None, client_id=None):
    """Production path: verify a Cognito ID token with the pool's JWKS (PyJWT)."""
    import jwt                                             # PyJWT, production only
    region = region or os.environ["COGNITO_REGION"]
    pool_id = pool_id or os.environ["COGNITO_USER_POOL_ID"]
    client_id = client_id or os.environ["COGNITO_CLIENT_ID"]
    issuer = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"
    key = jwt.PyJWKClient(issuer + "/.well-known/jwks.json").get_signing_key_from_jwt(token).key
    try:
        return jwt.decode(token, key, algorithms=["RS256"], audience=client_id, issuer=issuer)
    except jwt.PyJWTError as e:
        raise AuthError(str(e))


def verify_coordinator(token, store, secret=None):
    """Claims of a signed-in coordinator whose token is still live in the session store."""
    if os.environ.get("COGNITO_USER_POOL_ID"):
        claims = verify_cognito(token)
        claims.setdefault("jti", claims.get("origin_jti") or claims.get("event_id"))
    else:
        claims = verify_dev_token(token, secret or os.environ.get("SETUHAUL_DEV_SECRET", "dev-only-secret"))
    if not store.token_live(claims.get("jti")):
        raise AuthError("signed out or expired session")
    return claims
