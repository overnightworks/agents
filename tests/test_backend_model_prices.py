"""``GatewayBackend.model_prices`` reads a model's window and prices from the gateway's answer."""

from __future__ import annotations

import json
import traceback

import httpx2
import pytest

from agents.backend import TurnStopKind
from agents.backend.fake_gateway import FakeGateway, Reply, model_entry, model_info
from agents.backend.gateway import GatewayBackend
from agents.backend.model_prices import ModelPricesFailure, model_prices_in
from agents.turn import FailureCode, RejectionCode
from tests.support import gateway_turn_support
from tests.support.gateway_turn_support import (
    FailingTransport,
    a_model_prices,
    a_price_step,
)

scenario = gateway_turn_support.scenario

MODEL = "text/sonnet-medium"


def priced(scenario, *replies: Reply, model: str = MODEL):
    """The outcome of ``model_prices`` against the scripted gateway: prices, error and gateway."""
    gateway = FakeGateway(*replies)
    backend = GatewayBackend(scenario.config(), transport=gateway)
    prices, error = scenario.prices_outcome(backend, model)
    return prices, error, gateway


def test_a_listed_model_has_its_window_and_prices(scenario):
    expected = a_model_prices()

    prices, error, _ = priced(scenario, model_info(model_entry(MODEL, expected)))

    assert (prices, error) == (expected, None)


def test_the_request_is_one_get_with_the_apps_key_and_no_query_or_body(scenario):
    _, _, gateway = priced(scenario, model_info(model_entry(MODEL, a_model_prices())))

    (sent,) = gateway.requests
    assert (sent.method, sent.url.path, sent.url.query, sent.content) == (
        "GET",
        "/v1/model/info",
        b"",
        b"",
    )
    assert sent.headers["authorization"] == f"Bearer {scenario.key}"


def test_the_answer_of_a_gateway_with_other_models_gives_the_named_one(scenario):
    wanted = a_model_prices(input_per_token=3e-06)
    answer = model_info(
        model_entry("text/other", a_model_prices(input_per_token=9e-06)),
        model_entry(MODEL, wanted),
    )

    prices, _, _ = priced(scenario, answer)

    assert prices == wanted


def test_a_missing_one_hour_write_price_is_none(scenario):
    absent = a_model_prices(cache_write_1h_per_token=None)

    prices, _, _ = priced(scenario, model_info(model_entry(MODEL, absent)))

    assert prices == absent


def test_steps_come_back_ordered_by_threshold_with_the_prices_each_names(scenario):
    partial = a_price_step(
        200_000, output_per_token=None, cache_read_per_token=None, cache_write_1h_per_token=None
    )
    stepped = a_model_prices(steps=(a_price_step(100_000), partial))

    prices, _, _ = priced(scenario, model_info(model_entry(MODEL, stepped)))

    assert prices == stepped
    assert [step.above_prompt_tokens for step in prices.steps] == [100_000, 200_000]


def test_a_step_the_gateway_lists_unset_is_no_step(scenario):
    prices, _, _ = priced(scenario, model_info(model_entry(MODEL, a_model_prices())))

    assert prices.steps == ()


@pytest.mark.parametrize(
    "unpriced",
    ["input_per_token", "output_per_token", "cache_read_per_token", "cache_write_5m_per_token"],
)
def test_a_missing_base_price_is_the_missing_base_price_error(scenario, unpriced):
    answer = model_info(model_entry(MODEL, a_model_prices(), unpriced=[unpriced]))

    prices, error, _ = priced(scenario, answer)

    assert prices is None
    assert error.reason is ModelPricesFailure.BASE_PRICE_MISSING


@pytest.mark.parametrize(
    "absent",
    ["input_per_token", "output_per_token", "cache_read_per_token", "cache_write_5m_per_token"],
)
def test_a_base_price_the_entry_has_no_key_for_is_the_missing_base_price_error(scenario, absent):
    answer = model_info(model_entry(MODEL, a_model_prices(), absent=[absent]))

    prices, error, _ = priced(scenario, answer)

    assert prices is None
    assert error.reason is ModelPricesFailure.BASE_PRICE_MISSING


def test_a_model_the_answer_does_not_hold_is_the_not_listed_error(scenario):
    answer = model_info(model_entry("text/other", a_model_prices()))

    _, error, _ = priced(scenario, answer)

    assert error.reason is ModelPricesFailure.MODEL_NOT_LISTED


def test_entries_of_the_name_that_agree_give_their_prices(scenario):
    entry = model_entry(MODEL, a_model_prices())

    prices, _, _ = priced(scenario, model_info(entry, entry))

    assert prices == a_model_prices()


@pytest.mark.parametrize(
    "other",
    [a_model_prices(output_per_token=2e-05), a_model_prices(steps=(a_price_step(),))],
    ids=["a base price", "a step"],
)
def test_entries_of_the_name_that_differ_in_a_read_price_are_the_differing_entries_error(
    scenario, other
):
    answer = model_info(model_entry(MODEL, a_model_prices()), model_entry(MODEL, other))

    _, error, _ = priced(scenario, answer)

    assert error.reason is ModelPricesFailure.ENTRIES_DIFFER


def test_an_entry_with_a_missing_base_price_is_judged_before_the_entries_are_compared(scenario):
    unpriced = model_entry(MODEL, a_model_prices(), unpriced=["input_per_token"])
    answer = model_info(model_entry(MODEL, a_model_prices(output_per_token=2e-05)), unpriced)

    _, error, _ = priced(scenario, answer)

    assert error.reason is ModelPricesFailure.BASE_PRICE_MISSING


@pytest.mark.parametrize(
    ("reply", "kind", "code"),
    [
        (Reply(429, {"Retry-After": "7"}), TurnStopKind.REFUSED, RejectionCode.BUSY),
        (Reply(401), TurnStopKind.FAILED, FailureCode.PROVIDER_UNAVAILABLE),
        (Reply(404), TurnStopKind.FAILED, FailureCode.PROVIDER_UNAVAILABLE),
        (Reply(500), TurnStopKind.FAILED, FailureCode.PROVIDER_UNAVAILABLE),
    ],
    ids=["busy", "401", "404", "500"],
)
def test_a_status_other_than_200_is_the_stop_a_turn_would_end_with(scenario, reply, kind, code):
    prices, stopped, _ = priced(scenario, reply)

    assert prices is None
    assert (stopped.kind, stopped.code) == (kind, code)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b'{"data": {}}',
        b'{"data": [{"model_name": "text/sonnet-medium", "model_info": []}]}',
    ],
    ids=["not json", "not an object", "data not a list", "info not an object"],
)
def test_an_answer_that_cannot_be_read_is_a_malformed_reply(scenario, body):
    _, stopped, _ = priced(scenario, Reply(body=body))

    assert (stopped.kind, stopped.code) == (TurnStopKind.FAILED, FailureCode.PROVIDER_ERROR)


def test_a_window_the_answer_leaves_out_is_a_malformed_reply(scenario):
    entry = model_entry(MODEL, a_model_prices(), absent=["context_window_tokens"])

    _, stopped, _ = priced(scenario, model_info(entry))

    assert (stopped.kind, stopped.code) == (TurnStopKind.FAILED, FailureCode.PROVIDER_ERROR)


def test_a_lost_connection_is_provider_unavailable(scenario):
    backend = GatewayBackend(
        scenario.config(), transport=FailingTransport(httpx2.ConnectError("x"))
    )

    _, stopped = scenario.prices_outcome(backend)

    assert (stopped.kind, stopped.code) == (TurnStopKind.FAILED, FailureCode.PROVIDER_UNAVAILABLE)


def test_an_unusable_key_sends_nothing(scenario):
    gateway = FakeGateway(model_info(model_entry(MODEL, a_model_prices())))
    backend = GatewayBackend(scenario.config(key=b"two words\n"), transport=gateway)

    _, stopped = scenario.prices_outcome(backend)

    assert (stopped.code, gateway.requests) == (FailureCode.PROVIDER_UNAVAILABLE, [])


@pytest.mark.parametrize(
    "reply", [Reply(500), Reply(body=b"[]"), model_info()], ids=["500", "malformed", "not listed"]
)
def test_a_failed_read_holds_the_key_in_none_of_its_frames(scenario, reply):
    _, error, _ = priced(scenario, reply)

    frames = [frame for frame, _ in traceback.walk_tb(error.__traceback__)]
    held = [value for frame in frames for value in frame.f_locals.values()]
    assert scenario.key not in held
    assert error.__context__ is None


def test_a_closed_backend_raises_before_any_request(scenario):
    gateway = FakeGateway(model_info(model_entry(MODEL, a_model_prices())))
    backend = GatewayBackend(scenario.config(), transport=gateway)
    scenario.prices_outcome(backend)

    async def closed_read():
        await backend.aclose()
        await backend.model_prices(MODEL)

    with pytest.raises(RuntimeError) as raised:
        gateway_turn_support.in_virtual_time(closed_read())

    frames = [frame for frame, _ in traceback.walk_tb(raised.value.__traceback__)]
    held = [value for frame in frames for value in frame.f_locals.values()]
    assert scenario.key not in held


@pytest.mark.parametrize(
    "steps",
    [(a_price_step(200_000), a_price_step(100_000)), (a_price_step(), a_price_step())],
    ids=["descending", "one threshold twice"],
)
def test_steps_out_of_order_or_repeated_are_refused(steps):
    with pytest.raises(ValueError, match="steps must rise"):
        a_model_prices(steps=steps)


# LiteLLM's /model/info entry of a model with a one-hour cache write and one tier above 100k tokens.
WIRE_INFO = {
    "max_input_tokens": 200000,
    "input_cost_per_token": 1e-06,
    "output_cost_per_token": 5e-06,
    "cache_read_input_token_cost": 1e-07,
    "cache_creation_input_token_cost": 1.25e-06,
    "cache_creation_input_token_cost_above_1hr": 2e-06,
    "input_cost_per_token_above_100k_tokens": 2e-06,
    "output_cost_per_token_above_100k_tokens": 1e-05,
    "cache_read_input_token_cost_above_100k_tokens": 2e-07,
    "cache_creation_input_token_cost_above_100k_tokens": 2.5e-06,
    "cache_creation_input_token_cost_above_1hr_above_100k_tokens": 4e-06,
}
WIRE_PRICES = a_model_prices(
    context_window_tokens=200_000,
    input_per_token=1e-06,
    output_per_token=5e-06,
    cache_read_per_token=1e-07,
    cache_write_5m_per_token=1.25e-06,
    cache_write_1h_per_token=2e-06,
    steps=(
        a_price_step(
            100_000,
            input_per_token=2e-06,
            output_per_token=1e-05,
            cache_read_per_token=2e-07,
            cache_write_5m_per_token=2.5e-06,
            cache_write_1h_per_token=4e-06,
        ),
    ),
)


def test_the_reader_takes_the_key_names_litellm_writes():
    other_tier = {"input_cost_per_token_above_100k_tokens_batches": 1e-06, "unrelated": "x"}
    document = {"data": [{"model_name": MODEL, "model_info": {**WIRE_INFO, **other_tier}}]}

    assert model_prices_in(document, MODEL) == WIRE_PRICES


def test_the_fake_gateway_writes_the_key_names_litellm_writes():
    written = model_entry(MODEL, WIRE_PRICES)["model_info"]

    assert WIRE_INFO.items() <= written.items()
