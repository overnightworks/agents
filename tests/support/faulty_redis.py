"""Redis clients that fail the way a lost connection does, for the scripts of the listen record.

The first two reach the scripts through the client's ``evalsha`` and ``parse_response``, which
tandem's ``Script`` uses by its documented contract, so they match no key name or script text.
"""

from __future__ import annotations

from typing import Any

from fakeredis import FakeAsyncRedis
from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.asyncio.connection import ConnectionPool
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError


def retrying(times: int) -> Retry:
    """The ``Retry`` a client carries: it runs a failed command again ``times`` times at once."""
    return Retry(NoBackoff(), times)


class LostReplyRedis(Redis):
    """Raises a connection error in place of the reply of the first ``EVALSHA`` that returned: the
    script ran and its reply is lost, so a client with a ``Retry`` runs it again."""

    _reply_lost = False

    async def parse_response(self, connection: Any, command_name: Any, **options: Any) -> Any:
        reply = await super().parse_response(connection, command_name, **options)
        if command_name == "EVALSHA" and not self._reply_lost:
            self._reply_lost = True
            raise RedisConnectionError("the reply is lost")
        return reply


class EvalshaStopsAfter(Redis):
    """Lets the first ``evalshas_that_return`` calls of ``evalsha`` return and raises a connection
    error for every later one, so a pod that held a claim cannot finish what it began."""

    def __init__(self, url: str, *, evalshas_that_return: int) -> None:
        super().__init__(connection_pool=ConnectionPool.from_url(url))
        self._evalshas_that_return = evalshas_that_return

    async def evalsha(self, *arguments: Any, **options: Any) -> Any:
        if self._evalshas_that_return == 0:
            raise RedisConnectionError("the connection is gone")
        reply = await super().evalsha(*arguments, **options)
        self._evalshas_that_return -= 1
        return reply


class RefusedAfterConfirmationRedis(FakeAsyncRedis):
    """A fake Redis whose subscriptions are confirmed and then refused, as a revoked channel right
    refuses one."""

    def pubsub(self, **kwargs: Any) -> PubSub:
        subscription = super().pubsub(**kwargs)
        next_message = subscription.get_message
        confirmed = False

        async def refusing(*arguments: Any, **options: Any) -> Any:
            nonlocal confirmed
            if confirmed:
                raise ResponseError("NOPERM this user has no permissions to access the channel")
            confirmed = True
            return await next_message(*arguments, **options)

        subscription.get_message = refusing  # type: ignore[method-assign]
        return subscription
