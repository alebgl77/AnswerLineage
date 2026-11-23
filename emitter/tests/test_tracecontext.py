"""Un span de plus dans une trace : l'appel d'outil d'un agent, sous la trace de l'agent.

L'identifiant de run dérive du ``trace-id`` et du ``parent-id`` : deux spans
d'une même trace sont deux runs, et chacun se retrouve d'après son contexte.
"""

from __future__ import annotations

from collections.abc import Iterator

from answerlineage_emitter.tracecontext import (
    TraceContext,
    derive_run_id,
    new_span,
    new_trace_context,
    parse_traceparent,
)


def test_a_new_span_stays_in_the_trace_and_gets_its_own_parent_id() -> None:
    trace = new_trace_context()
    span = new_span(trace)
    assert span.trace_id == trace.trace_id
    assert span.parent_id != trace.parent_id
    assert derive_run_id(span) != derive_run_id(trace)
    # Le contexte se relit tel qu'il s'écrit : un autre émetteur le retrouve.
    assert parse_traceparent(span.traceparent()) == span


def test_a_new_span_never_has_a_null_parent_id() -> None:
    draws: Iterator[str] = iter(["0" * 16, "00f067aa0ba902b7"])
    span = new_span(
        TraceContext("4bf92f3577b34da6a3ce929d0e0e4736", "b7ad6b7169203331"),
        token=lambda _: next(draws),
    )
    assert span.parent_id == "00f067aa0ba902b7"
