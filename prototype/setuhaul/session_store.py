"""Sessions, tokens, dedupe, per-driver locks and recent history.

Core activity: hold the state that changes on every message, outside the decision code.
Production uses Redis (REDIS_URL). Without Redis (tests, the offline demo) the same
interface runs in memory, so nothing else in the code changes.

Redis is a cache, never the record. Bookings live only in the database; if Redis is
lost, sessions rebuild from the conversations and inbound_messages tables.

Keys (all values JSON text):
    session:drv:{phone}     driver_id, shipment_id, state, lang, last options   24 h after last message
    session:coord:{user_id} coordinator id, role, facility_id, token id          token lifetime
    token:{jti}             user_id                                             token lifetime (logout deletes it)
    hist:{phone}            last 20 messages, oldest first                      7 days
    dedupe:{wa_message_id}  "1"                                                 24 h
    lock:drv:{phone}        turn id                                             30 s
    rate:{phone}            message count in the current minute                 60 s

A driver's user id is their mobile number in E.164 form (+9198XXXXXXXX).
A coordinator's user id is their email, or their mobile number.
"""
import json
import os
import time
import uuid

SESSION_TTL_S = 24 * 3600
HISTORY_TTL_S = 7 * 24 * 3600
HISTORY_KEEP = 20
DEDUPE_TTL_S = 24 * 3600
LOCK_TTL_S = 30
RATE_WINDOW_S = 60
RATE_MAX = 20                 # messages per driver per minute before we stop processing


def normalise_phone(phone):
    """'98290 11111', '+91-98290-11111' and '919829011111' all become '+919829011111'."""
    digits = "".join(c for c in str(phone) if c.isdigit())
    if len(digits) == 10:
        digits = "91" + digits
    return "+" + digits


class _Memory:
    """The few Redis commands we use, in memory, with expiry. Single process only."""

    def __init__(self):
        self._d, self._exp = {}, {}

    def _alive(self, k):
        if k in self._exp and self._exp[k] <= time.time():
            self._d.pop(k, None)
            self._exp.pop(k, None)
        return k in self._d

    def get(self, k):
        return self._d.get(k) if self._alive(k) else None

    def set(self, k, v, ex=None, nx=False):
        if nx and self._alive(k):
            return None
        self._d[k] = v
        if ex:
            self._exp[k] = time.time() + ex
        else:
            self._exp.pop(k, None)
        return True

    def delete(self, *ks):
        n = 0
        for k in ks:
            if self._alive(k):
                n += 1
            self._d.pop(k, None)
            self._exp.pop(k, None)
        return n

    def incr(self, k):
        v = int(self.get(k) or 0) + 1
        self._d[k] = str(v)
        return v

    def expire(self, k, s):
        if self._alive(k):
            self._exp[k] = time.time() + s
        return True

    def ttl(self, k):
        if not self._alive(k):
            return -2
        return int(self._exp[k] - time.time()) if k in self._exp else -1


class SessionStore:
    def __init__(self, client=None):
        self.r = client if client is not None else _Memory()

    @classmethod
    def from_env(cls):
        """Redis when REDIS_URL is set and the redis package is installed; memory otherwise."""
        url = os.environ.get("REDIS_URL")
        if url:
            import redis                     # only needed in production
            return cls(redis.Redis.from_url(url, decode_responses=True))
        return cls()

    @property
    def backend(self):
        return "memory" if isinstance(self.r, _Memory) else "redis"

    # ---- driver session -------------------------------------------------------
    def load_driver(self, phone):
        return json.loads(self.r.get(f"session:drv:{normalise_phone(phone)}") or "{}")

    def save_driver(self, phone, session):
        self.r.set(f"session:drv:{normalise_phone(phone)}", json.dumps(session, default=str), ex=SESSION_TTL_S)

    # ---- coordinator session and tokens ----------------------------------------
    def start_coordinator(self, user_id, claims, ttl_s):
        jti = claims.get("jti") or uuid.uuid4().hex
        self.r.set(f"token:{jti}", user_id, ex=ttl_s)
        self.r.set(f"session:coord:{user_id}", json.dumps(dict(claims, jti=jti)), ex=ttl_s)
        return jti

    def token_live(self, jti):
        return self.r.get(f"token:{jti}") is not None

    def load_coordinator(self, user_id):
        return json.loads(self.r.get(f"session:coord:{user_id}") or "{}")

    def logout(self, jti):
        uid = self.r.get(f"token:{jti}")
        self.r.delete(f"token:{jti}")
        if uid:
            self.r.delete(f"session:coord:{uid}")

    # ---- history: human and AI messages as JSON ------------------------------------
    def append_history(self, phone, role, text, **meta):
        """role is 'human' (driver) or 'ai' (our reply, even when it came from a template)."""
        if role not in ("human", "ai"):
            raise ValueError("role must be 'human' or 'ai'")
        k = f"hist:{normalise_phone(phone)}"
        h = json.loads(self.r.get(k) or "[]")
        h.append(dict({"role": role, "text": text}, **meta))
        self.r.set(k, json.dumps(h[-HISTORY_KEEP:], default=str, ensure_ascii=False), ex=HISTORY_TTL_S)

    def history(self, phone):
        return json.loads(self.r.get(f"hist:{normalise_phone(phone)}") or "[]")

    # ---- dedupe, lock, rate limit ------------------------------------------------
    def first_time(self, wa_message_id):
        """True the first time a WhatsApp message id is seen (Meta redelivers webhooks)."""
        if not wa_message_id:
            return True
        return bool(self.r.set(f"dedupe:{wa_message_id}", "1", nx=True, ex=DEDUPE_TTL_S))

    def lock(self, phone):
        """One turn at a time per driver. Returns a token to release with, or None if busy."""
        tok = uuid.uuid4().hex
        return tok if self.r.set(f"lock:drv:{normalise_phone(phone)}", tok, nx=True, ex=LOCK_TTL_S) else None

    def unlock(self, phone, tok):
        k = f"lock:drv:{normalise_phone(phone)}"
        if self.r.get(k) == tok:
            self.r.delete(k)

    def allow(self, phone):
        k = f"rate:{normalise_phone(phone)}"
        n = self.r.incr(k)
        if n == 1:
            self.r.expire(k, RATE_WINDOW_S)
        return n <= RATE_MAX
