"""A member's lease across two pods of one host on one real Redis: a lease renewed on a pod that
was killed still holds on the other, with the generation it started under and the facts it kept,
and after its span the facts are gone and the other pod hands the lapse to its host once.

``scripts/run_redis_tests.sh`` runs this directory against a throwaway Redis. The lapse cell waits
out the lease in real seconds.
"""

from __future__ import annotations

import time

import httpx2

from agents.mcp_server.redis_place_store import CLAIM_FOR
from agents.member import LEASE_SECONDS
from tests.support.real_redis_pod import (
    FACTS_PATH,
    LAPSES_PATH,
    LEASE_PATH,
    LEASES_PATH,
    RENEW_PATH,
    WRITTEN_FACTS,
    WRITTEN_MEMBER,
)
from tests.support.real_redis_pods import WAIT_SECONDS, Pod, wait_until

_THE_MEMBER = [WRITTEN_MEMBER.iss, WRITTEN_MEMBER.sub]
_SWEEP_AND_CLAIM_SECONDS = 15


def renewed_on(pod: Pod) -> dict[str, object]:
    return httpx2.post(pod.url + RENEW_PATH, timeout=WAIT_SECONDS, trust_env=False).json()


def lease_standing_on(pod: Pod) -> dict[str, object]:
    return httpx2.get(pod.url + LEASE_PATH, timeout=WAIT_SECONDS, trust_env=False).json()


def facts_kept_on(pod: Pod) -> dict[str, dict[str, str]]:
    return httpx2.get(pod.url + FACTS_PATH, timeout=WAIT_SECONDS, trust_env=False).json()


def members_with_a_lease_on(pod: Pod) -> list[list[str]]:
    return httpx2.get(pod.url + LEASES_PATH, timeout=WAIT_SECONDS, trust_env=False).json()


def lapses_handed_to(pod: Pod) -> list[list[str]]:
    return httpx2.get(pod.url + LAPSES_PATH, timeout=WAIT_SECONDS, trust_env=False).json()


def test_a_lease_renewed_on_a_pod_that_was_killed_still_holds_on_the_other_with_its_generation(
    pods: tuple[Pod, Pod],
) -> None:
    pod_a, pod_b = pods
    renewal = renewed_on(pod_a)
    pod_a.kill()

    assert renewal["started"] is True
    assert lease_standing_on(pod_b) == {"holds": True, "generation": renewal["generation"]}
    assert members_with_a_lease_on(pod_b) == [_THE_MEMBER]
    assert facts_kept_on(pod_b) == {WRITTEN_MEMBER.sub: WRITTEN_FACTS.model_dump()}


def test_after_its_span_the_lapse_of_a_killed_pods_lease_reaches_the_other_pod_once(
    pods: tuple[Pod, Pod],
) -> None:
    pod_a, pod_b = pods
    generation = renewed_on(pod_a)["generation"]
    pod_a.kill()

    wait_until(
        lambda: lapses_handed_to(pod_b) == [[*_THE_MEMBER, generation]],
        seconds=LEASE_SECONDS + _SWEEP_AND_CLAIM_SECONDS,
    )
    time.sleep(2 * CLAIM_FOR.total_seconds())

    assert lapses_handed_to(pod_b) == [[*_THE_MEMBER, generation]]
    assert lease_standing_on(pod_b) == {"holds": False, "generation": None}
    assert members_with_a_lease_on(pod_b) == []
    assert facts_kept_on(pod_b) == {}
