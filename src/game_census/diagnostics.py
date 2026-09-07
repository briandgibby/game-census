"""Bounded exception context shared by durable workers; never exception text."""
import re


def exception_context(error, stage):
    """Allowlisted code locations, classes and SQLSTATE; no messages or locals.

    DatabaseError suppresses driver messages but retains safe cause metadata.
    Bounds protect reporting from cyclic or unusually deep exception chains.
    """
    chain, seen = [], set()
    while error is not None and id(error) not in seen and len(chain) < 8:
        seen.add(id(error))
        frames = []
        trace = error.__traceback__
        while trace is not None:
            module = trace.tb_frame.f_globals.get("__name__", "")
            if isinstance(module, str) and re.fullmatch(r"game_census(?:\.[a-z_]+)*", module):
                frames.append({"module": module, "function": trace.tb_frame.f_code.co_name,
                               "line": trace.tb_lineno})
            trace = trace.tb_next
        item = {"type": type(error).__name__, "frames": frames[-8:]}
        state = getattr(error, "sqlstate", None)
        if isinstance(state, str) and re.fullmatch(r"[A-Z0-9]{5}", state):
            item["sqlstate"] = state
        chain.append(item)
        error = error.__cause__ if error.__cause__ is not None else error.__context__
    return {"stage": stage, "exception_chain": chain}
