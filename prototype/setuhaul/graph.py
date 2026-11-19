"""The driver turn as a LangGraph StateGraph: the same steps as workflow.Session.run().

Core activity: orchestration only. Every node calls a step that already exists in
workflow.py; every edge depends on the turn's state, never on a model's choice.
LangGraph adds per-node tracing (LangSmith) and a place to add checkpoints later;
it does not change a single decision. Without langgraph installed, run_turn() runs
the same nodes in the same order with a plain loop.

    verify --(unknown sender)--> END
      |
    dedupe --(duplicate)--> END
      |
    read          rules first; LLM fallback only if the rules say UNKNOWN
      |
    safety --(safety words / asks for a person)--> END   (escalated)
      |
    dispatch      resolve shipment, ETA, re-plan, feasibility, book/hold/escalate,
      |           reply from the catalogue, output guard (inside Session.finish)
     END
"""
from typing import Any, Optional, TypedDict

from .workflow import Session
from .tracing import traced, tag_current_run


class TurnState(TypedDict, total=False):
    session: Any                  # workflow.Session (in-memory only; never checkpointed)
    history: list                 # stored JSON history, oldest first
    result: Optional[dict]        # set by the node that ends the turn


def _node(step):
    def run(state: TurnState) -> TurnState:
        return {"result": step(state["session"])}
    return run


def make_nodes(llm_fallback=None):
    return [
        ("verify", _node(lambda s: s.step_verify())),
        ("dedupe", _node(lambda s: s.step_dedupe())),
        ("read", lambda st: (st["session"].step_read(llm_fallback, st.get("history")), {"result": None})[1]),
        ("safety", _node(lambda s: s.step_safety())),
        ("dispatch", _node(lambda s: s.step_dispatch())),
    ]


def build_graph(llm_fallback=None):
    """Compile the LangGraph StateGraph. Raises ImportError if langgraph is not installed."""
    from langgraph.graph import StateGraph, END
    nodes = make_nodes(llm_fallback)
    g = StateGraph(TurnState)
    for name, fn in nodes:
        g.add_node(name, fn)
    g.set_entry_point("verify")
    names = [n for n, _ in nodes]
    for a, b in zip(names, names[1:]):
        if a == "read":
            g.add_edge(a, b)                                    # read never ends a turn
        else:
            g.add_conditional_edges(a, lambda st, b=b: END if st.get("result") is not None else b, [b, END])
    g.add_edge("dispatch", END)
    return g.compile()


_compiled = {}


def _graph_for(llm_fallback):
    key = id(llm_fallback)
    if key not in _compiled:
        try:
            _compiled[key] = build_graph(llm_fallback)
        except ImportError:
            _compiled[key] = None
    return _compiled[key]


@traced("driver_turn")
def run_turn(con, phone, text, as_of, external_id=None, history=None, llm_fallback=None, use_langgraph=True, capture=None):
    """One driver message -> one turn result. Same output as workflow.handle().

    capture: optional dict; receives the turn's evidence (docks, times, confirmed) so the channel
    layer can guard button titles against exactly what the tools returned. The result is unchanged."""
    session = Session(con, phone, text, as_of, external_id)
    state = {"session": session, "history": history or [], "result": None}
    graph = _graph_for(llm_fallback) if use_langgraph else None
    if graph is not None:
        out = graph.invoke(state)["result"]
    else:
        out = None
        for _, fn in make_nodes(llm_fallback):
            out = fn(state)["result"]
            if out is not None:
                break
    tag_current_run(out)
    if capture is not None:
        ev = getattr(session, "evidence", {}) or {}
        capture["evidence"] = {"docks": set(ev.get("docks", ())), "times": set(ev.get("times", ())),
                               "confirmed": bool(ev.get("confirmed"))}
    return out
