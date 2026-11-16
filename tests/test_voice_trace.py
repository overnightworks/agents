"""The voice loop answers each decision in the trace of the line that woke it."""

import pytest
from logline import unit_trace
from logline.trace import current_trace_id

from tests.support import voice_support
from tests.support.log_lines import log_lines
from tests.support.member_wire import a_waking_trace
from tests.support.voice_lines import a_line
from tests.support.voice_support import RETURNED, PacedGateway, finish, piece

place = voice_support.place
log_lines = log_lines

MODEL_REQUEST = "model_request.ended"


def trace_id_of(traceparent: str) -> str | None:
    with unit_trace(traceparent):
        return current_trace_id()


def trace_ids_sent(gateway: PacedGateway) -> list[str | None]:
    return [trace_id_of(request.headers["traceparent"]) for request in gateway.requests]


def test_each_answer_runs_in_the_trace_of_the_line_that_woke_it(place, log_lines):
    trace = place.trace
    first, second = a_waking_trace(), a_waking_trace()
    gateway = PacedGateway(trace, [piece("Gern."), finish()], [piece("Auch das."), finish()])

    async def hearing(trace):
        yield a_line(1, "Frage eins?").model_copy(update={"traceparent": first.traceparent})
        await trace.until(lambda: trace.played == ["Gern."])
        yield a_line(2, "Frage zwei?").model_copy(update={"traceparent": second.traceparent})
        await trace.settled()

    assert place.run(hearing, gateway) == RETURNED

    waking_trace_ids = [first.trace_id, second.trace_id]
    assert trace_ids_sent(gateway) == waking_trace_ids
    assert [line["trace_id"] for line in log_lines.of(MODEL_REQUEST)] == waking_trace_ids


@pytest.mark.parametrize("traceparent", [None, "not-a-traceparent"], ids=["absent", "malformed"])
def test_a_waking_line_without_a_usable_traceparent_starts_a_trace_of_its_own(
    place, log_lines, traceparent
):
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
    assert [line["trace_id"] for line in log_lines.of(MODEL_REQUEST)] == [sent]
