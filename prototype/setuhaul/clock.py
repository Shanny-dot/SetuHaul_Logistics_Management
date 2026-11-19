"""Time helpers. The snapshot has no single 'now', so every function takes as_of."""
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def ts(s):
    """ISO text -> aware datetime (None stays None)."""
    if s is None:
        return None
    if isinstance(s, datetime):
        return s
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=IST)


def iso(d):
    return None if d is None else d.astimezone(IST).isoformat(timespec="seconds")


def hhmm(d):
    return d.astimezone(IST).strftime("%H:%M")


def ampm(d):
    """12:30 -> '12:30 PM' (drivers read 12-hour time)."""
    return d.astimezone(IST).strftime("%I:%M %p").lstrip("0")


def add(d, minutes):
    return d + timedelta(minutes=minutes)


def minutes_between(a, b):
    return int(round((b - a).total_seconds() / 60))


def at(as_of, hh, mm=0):
    """Same day as as_of, given clock time."""
    return as_of.astimezone(IST).replace(hour=hh, minute=mm, second=0, microsecond=0)


class Clock:
    """The process clock. CLOCK_MODE=live: real IST time. CLOCK_MODE=snapshot: the snapshot day
    runs forward in real time from SNAPSHOT_ANCHOR (as_of = anchor + (now - process start)).
    One mode per process; jump_to() only moves a snapshot clock forward (demo time travel)."""

    def __init__(self, mode="live", anchor=None, _now=None):
        if mode not in ("live", "snapshot"):
            raise ValueError("CLOCK_MODE must be live or snapshot")
        if mode == "snapshot" and not anchor:
            raise ValueError("snapshot mode needs SNAPSHOT_ANCHOR")
        self.mode = mode
        self._real = _now or (lambda: datetime.now(IST))
        self.start = self._real()
        self.anchor = ts(anchor) if anchor else None

    @classmethod
    def from_env(cls, env=None):
        import os
        env = env if env is not None else os.environ
        return cls(env.get("CLOCK_MODE", "live"), env.get("SNAPSHOT_ANCHOR") or None)

    def real_now(self):
        return self._real()

    def now(self):
        if self.mode == "live":
            return self._real().astimezone(IST).replace(microsecond=0)
        return (self.anchor + (self._real() - self.start)).astimezone(IST).replace(microsecond=0)

    def jump_to(self, as_of):
        if self.mode != "snapshot":
            raise ValueError("only a snapshot clock can jump")
        target = ts(as_of)
        if target < self.now():
            raise ValueError("a snapshot clock never goes back (the database already moved on)")
        self.anchor, self.start = target, self._real()

    def describe(self):
        return {"clock_mode": self.mode, "as_of": iso(self.now())}
