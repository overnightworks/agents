"""The voice loop answers each decision in the trace of the line that woke it."""

import pytest
from logline import unit_trace
from logline.trace import current_trace_id

from tests.support import voice_support
from tests.support.voice_lines import a_line
from tests.support.voice_support import RETURNED, PacedGateway, finish, piece

place = voice_support.place

A_TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def trace_ids_sent(gateway: PacedGateway) -> list[str]:
    return [request.headers["traceparent"].split("-")[1] for request in gateway.requests]


def test_an_answer_runs_in_the_trace_of_the_line_that_woke_it(place):
    trace = place.trace
    gateway = PacedGateway(trace, [piece("Gern."), finish()])
    waking_trace_id = A_TRACEPARENT.split("-")[1]

    async def hearing(trace):
        yield a_line(1, "Frage eins?").model_copy(update={"traceparent": A_TRACEPARENT})
        await trace.settled()

    assert place.run(hearing, gateway) == RETURNED

    assert trace_ids_sent(gateway) == [waking_trace_id]


def test_each_answer_runs_in_the_trace_of_the_line_that_woke_it(place):
    trace = place.trace
    other = A_TRACEPARENT.replace("4bf92f35", "5cf92f35")
    gateway = PacedGateway(trace, [piece("Gern."), finish()], [piece("Auch das."), finish()])

    async def hearing(trace):
        yield a_line(1, "Frage eins?").model_copy(update={"traceparent": A_TRACEPARENT})
        await trace.until(lambda: trace.played == ["Gern."])
        yield a_line(2, "Frage zwei?").model_copy(update={"traceparent": other})
        await trace.settled()

    assert place.run(hearing, gateway) == RETURNED

    assert trace_ids_sent(gateway) == [t.split("-")[1] for t in (A_TRACEPARENT, other)]


@pytest.mark.parametrize("traceparent", [None, "not-a-traceparent"], ids=["absent", "malformed"])
def test_a_waking_line_without_a_usable_traceparent_starts_a_trace_of_its_own(place, traceparent):
    gateway = PacedGateway(place.trace, [piece("Gern."), finish()])

    async def hearing(trace):
        yield a_line(1, "Frage eins?").model_copy(update={"traceparent": traceparent})
        await trace.settled()

    with unit_trace(None):
        enclosing_trace_id = current_trace_id()
        ending = place.run(hearing, gateway)

    (sent,) = trace_ids_sent(gateway)
    assert ending == RETURNED
    assert len(sent) == 32
    assert sent != enclosing_trace_id
