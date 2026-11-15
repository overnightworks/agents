"""``GatewayBackend`` keeps one pooled client over a loopback gateway that counts connections."""

from __future__ import annotations

import asyncio

import pytest

from agents.backend.fake_gateway import FakeGateway, Reply, completion
from agents.backend.gateway import GatewayBackend, GatewayDetail
from agents.turn_events import FinalEvent
from tests.support import gateway_turn_support
from tests.support.gateway_turn_support import UNAVAILABLE, _turn, a_request
from tests.support.keep_alive_gateway import serving_keep_alive

scenario = gateway_turn_support.scenario


def test_a_cookie_the_gateway_sets_is_not_sent_back_over_a_pooled_connection(scenario):
    async def drive():
        async with serving_keep_alive(set_cookie="session=1") as gateway:
            async with GatewayBackend(scenario.config(origin=gateway.origin)) as backend:
                for _ in range(2):
                    await _turn(backend, a_request(), scenario.person()[0])
            return gateway.cookies

    assert asyncio.run(drive()) == [None, None]


def test_consecutive_turns_share_one_connection(scenario):
    async def drive():
        async with serving_keep_alive() as gateway:
            async with GatewayBackend(scenario.config(origin=gateway.origin)) as backend:
                outcomes = [await _turn(backend, a_request(), scenario.person()[0]) for _ in "ab"]
            return [stopped for _, stopped in outcomes], gateway.accepted

    assert asyncio.run(drive()) == ([None, None], 1)


def test_a_connection_the_gateway_closed_while_idle_still_serves_the_next_turn(scenario):
    async def drive():
        async with serving_keep_alive() as gateway:
            async with GatewayBackend(scenario.config(origin=gateway.origin)) as backend:
                await _turn(backend, a_request(), scenario.person()[0])
                await gateway.close_idle()
                events, stopped = await _turn(backend, a_request(), scenario.person()[0])
            return events[-1], stopped, gateway.accepted

    assert asyncio.run(drive()) == (FinalEvent(text="hi"), None, 2)


def test_closing_the_backend_ends_its_connection_and_refuses_further_turns(scenario):
    async def drive():
        async with serving_keep_alive() as gateway:
            backend = GatewayBackend(scenario.config(origin=gateway.origin))
            await _turn(backend, a_request(), scenario.person()[0])
            await backend.aclose()
            await gateway.wait_until_ended(1)
            with pytest.raises(RuntimeError):
                await _turn(backend, a_request(), scenario.person()[0])
            return gateway.ended

    assert asyncio.run(drive()) == 1


def test_a_redirect_is_not_followed_and_a_cookie_is_not_kept(scenario):
    gateway = FakeGateway(
        Reply(302, {"Location": "http://elsewhere.test/", "Set-Cookie": "session=1"}),
        completion("ok"),
    )
    backend = GatewayBackend(scenario.config(), transport=gateway)

    async def both_in_one_loop():
        async with backend:
            first = await _turn(backend, a_request(), scenario.person()[0])
            await _turn(backend, a_request(), scenario.person()[0])
            return first

    _, first = asyncio.run(both_in_one_loop())

    assert (first.kind, first.code, first.detail) == (*UNAVAILABLE, GatewayDetail.GATEWAY_FAILED)
    assert [sent.url.host for sent in gateway.requests] == ["gateway.test", "gateway.test"]
    assert "cookie" not in gateway.requests[1].headers
