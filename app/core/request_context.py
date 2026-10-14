from contextvars import ContextVar, Token

trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")


def get_trace_id() -> str:
    return trace_id_var.get()


def set_trace_id(trace_id: str) -> Token[str]:
    return trace_id_var.set(trace_id)
