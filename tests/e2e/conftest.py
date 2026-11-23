"""The throwaway Redis of ``scripts/run_redis_tests.sh``, which passes its URL in
``AGENTS_TEST_REDIS_URL``."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from redis import Redis

from tests.support.real_redis_pods import POD_A_PASSWORD, POD_A_USER, Pod, as_user, started


@pytest.fixture(scope="session")
def redis_url() -> str:
    url = os.environ.get("AGENTS_TEST_REDIS_URL")
    if url is None:
        pytest.fail("AGENTS_TEST_REDIS_URL is unset; run scripts/run_redis_tests.sh")
    return url


@pytest.fixture
def admin(redis_url: str) -> Iterator[Redis]:
    """The runner's own client: a different user from either pod, on an empty Redis."""
    client = Redis.from_url(redis_url, decode_responses=True)
    client.flushall()
    yield client
    client.flushall()
    client.close()


@pytest.fixture
def pod_a_user(admin: Redis) -> Iterator[str]:
    """The Redis user pod A connects as, with every right; a test takes some away."""
    admin.acl_setuser(
        POD_A_USER,
        enabled=True,
        passwords=[f"+{POD_A_PASSWORD}"],
        keys=["*"],
        channels=["*"],
        commands=["+@all"],
    )
    try:
        yield POD_A_USER
    finally:
        admin.acl_deluser(POD_A_USER)


@pytest.fixture
def pods(tmp_path: Path, redis_url: str, pod_a_user: str) -> Iterator[tuple[Pod, Pod]]:
    """Pod A as its own Redis user, pod B as the default one."""
    pod_a = started(tmp_path, as_user(redis_url, pod_a_user, POD_A_PASSWORD))
    try:
        pod_b = started(tmp_path, redis_url)
        try:
            yield pod_a, pod_b
        finally:
            pod_b.kill()
    finally:
        pod_a.kill()
