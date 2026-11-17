# agents

## Commands

- `uv sync --locked --extra dev`: install the environment; run it before any other command.
- `uv run pytest -q -n auto`: the suite; needs no live provider.
- `scripts/run_redis_tests.sh [pytest argument...]`: the tests of `tests/integration/real_redis` (two processes, and one that waits out a claim) against a throwaway Redis; the default suite skips that directory. Run it under `probe-run`.
- `uv run ruff check src tests scripts`: lint `src`, `tests` and `scripts`.
- `uv run pyright`: type-check `src` and `scripts` in strict mode.
- `uvx --from "git+https://github.com/overnightworks/quality@<sha>" quality check`: the gate's local check; `<sha>` is the one `.github/workflows/ci.yml` pins for quality's gate.
- `uv run python scripts/generate_figure_vocabulary.py`: regenerate `src/agents/figure_vocabulary.py` from the figure release pinned in `scripts/figure_vocabulary_pin.toml`; CI requires an empty diff. Add `--pin <url> <integrity>` to raise the pin to a newer release.
- `uv run python scripts/generate_member_tool_wire.py`: regenerate `scripts/member-tool-wire.json` from `MEMBER_TOOL_ARGUMENTS` and `MEMBER_TOOL_RESULTS`; CI requires an empty diff.
- `uv build`: build the wheel; run it after `uv sync`.
- `gh workflow run release.yml -f version=X.Y.Z`: dispatch a release; the pull request before it raises `version` in `pyproject.toml`.

## Rules

- The turn's vocabulary has one owner, `agents.turn`; `turn_events` is the in-process event, never a wire format.
- The libraries of the `backend` and `mcp-server` extras are imported only inside the module that needs them; `.importlinter` forbids them in the client's base modules.
- `tandem` is imported only by `agents.mcp_server.redis_place_store`, which `agents.mcp_server` does not re-export; no module of `agents` imports `redis`, which tandem brings.
- `src/agents/figure_vocabulary.py` is generated, never edited; figure owns the gestures and moods in it.
- Deployment facts (the gateway origin and key file) come from the host's `AgentConfig`, never from an environment variable or a module default.
- Host obligations (the app's `HandTool`s, the MCP person front's handlers) are ports passed in; the library never invents them.
- Tests build their fakes with the builders in `tests/support`, not a second set.
- No host word appears in `src`, `tests` or `docs`; the "Host vocabulary" step in `.github/workflows/ci.yml` holds the list.
- A tag is never moved; a broken release gets the next patch tag.

## Documents

- `README.md`: read when you need what the library is, how a host installs it and its extras.
- `docs/VISION.md`: read when you judge whether a change belongs in this library at all.
- `docs/ARCHITECTURE.md`: read when you change the shape of the client or must know why it was built so.
- `docs/calling.md`: read when you build or test an app's way for a person to call an agent to a place and send it away.
- `docs/hearing.md`: read when you build or test an app's way to let an agent hear what people say, or see, at a place.
- `docs/agent-kinds.md`: read when an app adds an agent surface and you must decide helper or persona, or who owns a part.
- `docs/connector.md`: read when you change what an app sees: the turn, agent files, the tool contract, the gateway backend or the MCP person front.
