"""The throwaway Redis of ``scripts/run_redis_tests.sh``, which passes its URL in
``AGENTS_TEST_REDIS_URL``."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from redis import Redis


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
