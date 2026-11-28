#!/usr/bin/env bash
# Runs the tests of tests/e2e against a throwaway Redis: scripts/run_redis_tests.sh [pytest argument...]
#
# The tests find the server in AGENTS_TEST_REDIS_URL. The container goes when the run ends, is stopped
# or the caller dies; an image this run pulled goes with it. Keep the image and the server arguments
# equal to the platform base's.
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE='docker.io/library/redis:7.4.12-alpine@sha256:338fbfc036dc5517ac0b04d6de21521085935336e565f223406180e9d84166e2'
# The cap and its 30 s grace are the repository-wide rule; the container stops within the grace.
CAP_SECONDS=1800
GRACE_SECONDS=30
LOAD_WAIT_SECONDS=1800
READY_WAIT_SECONDS=60

# Re-run this script under the cap, so that the trap below also runs when the cap or the caller's death ends it.
if [[ -z "${AGENTS_REDIS_TESTS_CAPPED:-}" ]]; then
    AGENTS_REDIS_TESTS_CAPPED=1 exec setpriv --pdeathsig TERM timeout -k "${GRACE_SECONDS}" "${CAP_SECONDS}" "$0" "$@"
fi

# Exit 0 when the load is above the limit, 1 when not; any other awk status ends the script.
load_above_limit() {
    local current="$1" limit="$2" status=0
    awk -v current="${current}" -v limit="${limit}" 'BEGIN { exit !(current > limit) }' || status=$?
    case "${status}" in
        0) return 0 ;;
        1) return 1 ;;
        *)
            echo "could not compare load ${current} with limit ${limit}: awk exited ${status}" >&2
            exit 2
            ;;
    esac
}

read_load() {
    cut -d' ' -f1 /proc/loadavg || { echo 'could not read /proc/loadavg' >&2; exit 2; }
}

wait_for_low_load() {
    local limit waited=0 load
    limit=$(awk -v cores="$(nproc)" 'BEGIN { print 1.5 * cores }') ||
        { echo 'could not compute the load limit' >&2; exit 2; }
    load=$(read_load) || exit 2
    while load_above_limit "${load}" "${limit}"; do
        if [[ "${waited}" -ge "${LOAD_WAIT_SECONDS}" ]]; then
            echo "load ${load} stayed above ${limit}" >&2
            exit 1
        fi
        sleep 10
        waited=$((waited + 10))
        load=$(read_load) || exit 2
    done
}

wait_for_low_load

pulled_image=''
if ! docker image inspect "${IMAGE}" >/dev/null 2>&1; then
    docker pull --quiet "${IMAGE}" >/dev/null
    pulled_image=1
fi

container="agents-test-redis-$$"
cleanup() {
    docker rm --force "${container}" >/dev/null 2>&1 || true
    if [[ -n "${pulled_image}" ]]; then
        docker rmi "${IMAGE}" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT TERM

docker run --quiet --detach --name "${container}" --publish 127.0.0.1::6379 "${IMAGE}" \
    --save '' --appendonly no --maxmemory-policy noeviction >/dev/null

for _ in $(seq "${READY_WAIT_SECONDS}"); do
    [[ "$(docker exec "${container}" redis-cli ping 2>/dev/null || true)" == PONG ]] && break
    sleep 1
done
docker exec "${container}" redis-cli ping >/dev/null

port=$(docker port "${container}" 6379/tcp | head -n 1 | sed 's/.*://')
# Run as a child, not exec'd, so that the trap above survives the command.
AGENTS_TEST_REDIS_URL="redis://127.0.0.1:${port}" \
    uv run --no-sync pytest tests/e2e "$@"
