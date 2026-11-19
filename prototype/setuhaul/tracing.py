"""LangSmith tracing, with phone numbers masked. A no-op when LangSmith is off.

Core activity: make every driver turn reviewable afterwards, without leaking personal data.

Turn it on with LANGSMITH_TRACING=true and LANGSMITH_API_KEY (optionally LANGSMITH_PROJECT).
Each turn becomes one trace; LangGraph nodes and the LLM fallback appear as child runs.
Guardrail results are added as tags: guard:blocked, safety, option_gone, escalated, duplicate.
"""
import os
import re

PHONE = re.compile(r"\+?\d[\d\s-]{8,}\d")


def mask(text):
    """'+91-9000010001' -> '+91-90******01'. Applied to every traced input and output."""
    def _m(mo):
        s = mo.group(0)
        keep = [i for i, c in enumerate(s) if c.isdigit()]
        hide = set(keep[4:-2])
        return "".join("*" if i in hide else c for i, c in enumerate(s))
    return PHONE.sub(_m, text) if isinstance(text, str) else text


def _mask_obj(o):
    if isinstance(o, dict):
        return {k: _mask_obj(v) for k, v in o.items() if k not in ("con", "session")}
    if isinstance(o, (list, tuple)):
        return [_mask_obj(v) for v in o]
    return mask(o) if isinstance(o, str) else o


def enabled():
    return os.environ.get("LANGSMITH_TRACING", os.environ.get("LANGCHAIN_TRACING_V2", "")).lower() == "true"


def tags_for(result):
    tools = " ".join(result.get("tools") or [])
    t = []
    if "output_guard:BLOCKED" in tools:
        t.append("guard:blocked")
    if "SAFETY" in tools:
        t.append("safety")
    if "OPTION_GONE" in tools:
        t.append("option_gone")
    if result.get("state") == "ESCALATED":
        t.append("escalated")
    if "dedupe:SUPPRESSED" in tools:
        t.append("duplicate")
    if "llm_fallback" in tools:
        t.append("llm_fallback")
    return t


def traced(name):
    """Decorator: a LangSmith run when tracing is on and langsmith is installed; otherwise the function unchanged."""
    def deco(fn):
        if not enabled():
            return fn
        try:
            from langsmith import traceable
        except ImportError:
            return fn
        return traceable(name=name, process_inputs=_mask_obj, process_outputs=_mask_obj)(fn)
    return deco


def tag_current_run(result):
    """Attach guardrail tags to the current LangSmith run, if any."""
    if not enabled():
        return
    try:
        from langsmith.run_helpers import get_current_run_tree
        rt = get_current_run_tree()
        if rt is not None:
            rt.tags = list(set((rt.tags or []) + tags_for(result)))
    except Exception:
        pass
