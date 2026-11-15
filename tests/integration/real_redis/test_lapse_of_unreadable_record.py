"""The lapse script on a real Redis, where ``cjson.decode`` raises on text that is no JSON; the
fake Redis raises a Python error there that no real server shows."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta

import pytest
from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from tandem.keys import deadline_keys

from agents.mcp_server.asgi import LOGGER_NAME
from agents.mcp_server.place_store import ListeningTurn, ResourceChange, Resubscribed
from agents.mcp_server.redis_place_store import RedisPlaceStore
from agents.member import STANDING_URI
from tests.support.mcp_server_support import a_member
from tests.support.redis_listen_support import delivered_up_to, eventually, subscribed

_PREFIX = "notes"
_ALICE = a_member("alice")
_STREAMS = f"{_PREFIX}-agents:v1:streams"
_LISTENS = f"{_PREFIX}-agents:v1:listens"
_CLAIMED = deadline_keys(f"{_PREFIX}-agents", "listen").claimed


@pytest.mark.parametrize(
    "record",
    [
        pytest.param("no json", id="a record that is no json"),
        pytest.param(
            json.dumps(["no json", [STANDING_URI]]), id="a principal text that is no json"
        ),
    ],
)
def test_a_lapse_on_a_record_that_does_not_decode_writes_nothing_and_logs(
    redis_url: str, admin: Redis, record: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=LOGGER_NAME)

    async def scenario() -> tuple[list[object], dict[str, str], dict[str, int]]:
        store = RedisPlaceStore(AsyncRedis.from_url(redis_url), key_prefix=_PREFIX)
        async with subscribed(store) as events:
            await store.acknowledged("held", _ALICE, [STANDING_URI], timedelta(milliseconds=1))
            admin.hset(_STREAMS, "held", record)

            async def claimed() -> bool:
                await store.lapse_due("pod")
                return bool(admin.zscore(_CLAIMED, "held"))

            await eventually(claimed, seconds=10)
            seen = await delivered_up_to(
                store, events, ResourceChange(a_member("bob"), STANDING_URI, "test")
            )
        return seen, admin.hgetall(_STREAMS), admin.zrange(_LISTENS, 0, -1)

    seen, streams, listens = asyncio.run(scenario())

    assert streams == {"held": record}
    assert len(listens) == 1
    assert ListeningTurn(_ALICE, STANDING_URI, listens=False) not in seen
    assert Resubscribed() in seen
    assert caplog.records
