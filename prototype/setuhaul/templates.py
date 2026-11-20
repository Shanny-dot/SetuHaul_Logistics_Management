"""Template registry: which approved template carries a business-initiated message.

templates/twilio_templates.yaml holds the bodies to submit for approval; the content SID of each
approved template comes from the environment (TWILIO_TPL_<NAME>_SID). No SID -> no send.
"""
import os

from .languages import pick

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(HERE, "templates", "twilio_templates.yaml")
_cache = {}


def load(path=PATH):
    if path not in _cache:
        try:
            import yaml
        except ImportError:                     # pyyaml missing: no templates, so proactive sends are blocked
            _cache[path] = {}
        else:
            with open(path, encoding="utf-8") as f:
                _cache[path] = (yaml.safe_load(f) or {}).get("templates", {})
    return _cache[path]


def variables(name, values, sandbox=False):
    """Named values -> Twilio content_variables {'1': ..., '2': ...} for template `name`."""
    t = load().get(name)
    if not t:
        raise LookupError(f"unknown template {name}")
    if sandbox and t.get("sandbox_vars"):
        return {k: str(values.get(v, "")) for k, v in t["sandbox_vars"].items()}
    return {str(i): str(values.get(v, "")) for i, v in enumerate(t["vars"], 1)}


def preview(name, values, lang="en"):
    """The text the template will show (for the trace, the simulator and the guard)."""
    t = load().get(name) or {}
    bodies = t.get("body") or {}
    body = (pick(bodies, lang) if bodies else None) or name          # gu/mr/pa/mwr -> hi -> en if not approved yet
    for i, v in enumerate(t.get("vars", []), 1):
        body = body.replace("{{%d}}" % i, str(values.get(v, "")))
    return body
