"""A member's lease, kept in the place store so that it survives the pod that renewed it.

``MemberLeases`` is what a host calls: ``renew`` on every admitted call of a member, with the
facts its verified token states, ``state``, ``live`` and ``facts`` to read, and ``running()`` once
in the host's lifespan to hand each lapse to the host's ``lease_ended``. The lease and its lapse
queue live in the ``LeaseRecord`` the host passes in; with more than one pod that is
``agents.mcp_server.redis_place_store.RedisPlaceStore``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Collection, Mapping
from contextlib import asynccontextmanager
from typing import Final

from agents.mcp_server.asgi import LOGGER_NAME
from agents.mcp_server.place_store import (
    LeaseRecord,
    LeaseRenewal,
    LeaseRoster,
    LeaseStanding,
    PlaceStoreError,
    failure_type,
)
from agents.member import MemberFacts
from agents.turn import Principal

_LOG: Final = logging.getLogger(LOGGER_NAME)

LAPSE_RESTART_SECONDS: Final = 5


class MemberLeases:
    """How an app keeps, reads and lists a member's lease, and hears of each lapse.

    Every method raises ``PlaceStoreError`` when the store fails. ``lease_ended(principal,
    generation)`` is called on one live pod for each lapse, at least once, and again after the
    store's claim has run out when it raised, so a host fences its action by the generation in one
    atomic step of its own store and applies it idempotently. A host compares a generation by
    equality only, never shows it to a person and never logs it; no line of this class carries one.

    ``member_lease.opened`` is written when a renewal started a lease and ``member_lease.lapsed``
    for each lapse handed over, which may repeat as the handover may.
    """

    def __init__(
        self, store: LeaseRecord, *, lease_ended: Callable[[Principal, str], Awaitable[None]]
    ) -> None:
        self._store = store
        self._lease_ended = lease_ended
        self._failed_run_count = 0

    async def renew(self, principal: Principal, facts: MemberFacts) -> LeaseRenewal:
        """Keeps the principal's lease and ``facts`` for another ``LEASE_SECONDS``. Async, so a
        host calls it from an async site, never from the synchronous ``resolve_person``.

        ``started`` is true when a new lease began; a host treats a generation other than the one
        it stored as a start, because the store may replay a renewal as ``started=False``. An
        owner change replaces the facts and keeps the lease. Two pods renewing one member at once
        may store their facts out of order, as the last write wins; the next renewal corrects it.
        A renewal that raises may have kept the lease without its facts.
        """
        renewal = await self._store.renew(principal, facts)
        if renewal.started:
            _LOG.info("member_lease.opened", extra={"sub": principal.sub})
        return renewal

    async def state(self, principal: Principal) -> LeaseStanding:
        """The principal's lease from one snapshot, renewing nothing.

        ``holds`` is true while the lease runs and, right after the store lost its data, for
        ``LEASE_SECONDS`` with no generation: an add accepted then is caught only by the host's own
        reconcile.
        """
        return await self._store.state(principal)

    async def facts(self, principals: Collection[Principal]) -> Mapping[Principal, MemberFacts]:
        """The facts of each principal that has some, in one read whatever the count. A principal
        is left out when it never renewed, its lease lapsed, the store lost its data, or its
        latest renewal raised after the lease step. An empty batch answers ``{}`` with no store
        call.

        A stored value that cannot be read raises ``PlaceStoreError`` with a text that carries
        none of it, and fails every batch read that includes its principal until that agent
        renews. A host never logs the facts.
        """
        return await self._store.facts(principals)

    async def live(self) -> LeaseRoster:
        """The principals whose lease runs; while the roster is not ``settled`` a principal that
        is missing may have lost its lease with the store."""
        return await self._store.live()

    @asynccontextmanager
    async def running(self) -> AsyncGenerator[None, None]:
        """The lapse service as a task of its own, for as long as the block runs; the host enters
        it once in its lifespan, on every pod.

        A failure of the service never ends it silently: the first of a streak is written by type
        (``lease_lapse_service.failed``, a warning), and the service serves again after
        ``LAPSE_RESTART_SECONDS``. A streak ends when a restarted service ran longer than that
        without failing; ``lease_lapse_service.resumed`` then writes its ``failed_run_count``.
        The block's exit cancels the service and waits for it, so a handler still running is cut and
        its lapse returns after the store's claim has run out.
        There is no readiness signal: a failing service is only written.
        """
        service = asyncio.create_task(self._serve())
        try:
            yield
        finally:
            service.cancel()
            await asyncio.wait((service,))

    async def _serve(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            resumption = (
                loop.call_later(LAPSE_RESTART_SECONDS, self._log_resumed)
                if self._failed_run_count
                else None
            )
            try:
                await self._store.serve_lapses(self._lapsed)
            except Exception as failure:
                if not self._failed_run_count:
                    self._log_failure(failure)
                self._failed_run_count += 1
            finally:
                if resumption is not None:
                    resumption.cancel()
            await asyncio.sleep(LAPSE_RESTART_SECONDS)

    def _log_resumed(self) -> None:
        _LOG.info("lease_lapse_service.resumed", extra={"failed_run_count": self._failed_run_count})
        self._failed_run_count = 0

    async def _lapsed(self, principal: Principal, generation: str) -> None:
        _LOG.info("member_lease.lapsed", extra={"sub": principal.sub})
        await self._lease_ended(principal, generation)

    @staticmethod
    def _log_failure(failure: Exception) -> None:
        _LOG.warning(
            "lease_lapse_service.failed",
            extra={"error_type": failure_type(failure)},
            exc_info=None if isinstance(failure, PlaceStoreError) else failure,
        )
