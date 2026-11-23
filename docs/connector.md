# Connector reference

For the developer of an app that runs agent turns through `agents`: the turn an
app starts, the agent files it ships, the tools it declares, the gateway backend, the MCP
person front and the voice loop. The standard path is that the app serves its member's standing as MCP resources and
announces a change through the standard's subscriptions; [the standard path](#the-standard-path)
says what a host wires and what a runner relies on. Each rule names the test that proves it. The
docstrings of `agent_file`, `tools`, `turn`, `backend` and `mcp_server` are exact where this file is
short; the shape of the client is in [ARCHITECTURE.md](ARCHITECTURE.md).

## The turn

An app runs a turn through `agents.backend.AgentBackend`, installed as
`src/<app>/agent/backend.py` with its tools in `src/<app>/agent/tools.py`.
`backend.run(request, person)` is an async generator of `TurnEvent`s that the app consumes inside
`contextlib.aclosing`; closing it runs the backend's cleanup. The base modules import no HTTP
library; the one implementation, `backend.gateway.GatewayBackend`, needs the `backend` extra.

`request` is a `TurnRequest` (`agents.agent_file`, frozen): `agent`, `turn`, `roles`,
`tools` (the agent's selected `ToolDeclaration`s), `model`, `max_duration_seconds`,
`max_tool_calls` (`None` exactly when `tools` is empty), `picture_size` (`None` for a text
agent) and `deferred_tools` (see "Deferred tools and tool rounds" below).
`AgentDefinition.request(messages, *, model=None, appended_system=None,
one_call_per_reply=False)` builds it; `model` overrides the file's model for that run, with a
model of the agent's kind, and `appended_system` follows the file's prompt in the system text,
after a blank line. `one_call_per_reply` (`TurnRequest.one_call_per_reply`, default `False`)
asks for one tool call per model reply; the backend serves the first call of a reply and answers
the rest as not run, as the tool loop describes, and the setting does nothing for an agent
without tools. Every further act then takes one more model round. The request's body is the same
with or without it.
Proved by `tests/test_agent_file.py`: `test_request_carries_the_files_prompt_model_roles_and_tools`,
`test_request_model_is_the_override_when_given`, `test_request_is_frozen`,
`test_request_carries_one_call_per_reply_only_when_asked`; the appended text by
`tests/test_voice_loop.py`: `test_a_spoken_line_that_wakes_sends_one_request_with_prompt_protocol_context_and_the_line`.

**Deferred tools and tool rounds.** `TurnRequest.deferred_tools` (default none) names tools of
`tools` that are sent with `"defer_loading": true`, in the order `tools` has; a tool loads when a
result names it in `ToolOutcome.loads`. The request refuses with `ValueError`, before anything is
sent: a deferred name that is not in `tools`, every tool deferred (a request with no tools is
valid), a recorded round's `loads` name that is not deferred, and a picture request that holds a
`ToolRound`. The backend validates the request again at `run`, so a `model_copy` cannot bypass
these. `Turn.messages` holds `TurnMessage`s and `ToolRound`s. A `ToolRound` is one model reply
that asked for tools: its `text` (or none) and its `RoundCall`s in reply order, each with the
`call_id`, `tool`, `arguments` (the JSON text of the object, as the model spelled it) and the
answer the model was shown (`result`, `is_error`, `image`, `loads`), the calls one call per reply did not run included as errors with the not-run text.
`FinalEvent.tool_rounds` holds every tool round of the turn, images included. A host records the
rounds of every finished turn, because leaving one out breaks the cached prefix at the first round
left out, and replays them as `Turn.messages` before the turn's answer; the next turn then sends
the last request of the one before, byte for byte, as its prefix. A tool that a round loaded stays
in `deferred_tools` when the round is replayed. Replayed rounds do not count against
`max_tool_calls`; only the calls of the turn's own rounds do. A stopped turn has no `FinalEvent`,
so its rounds are not recorded, and replay resumes at the next finished turn. `Turn` refuses a call
id that appears twice across its rounds, and a replayed id that the model reuses ends the round
`failed: provider_error`, detail `malformed_reply`. `call_id` is only non-empty, because the
gateway mints the ids of a turn; a host that writes a round mints ids from `[A-Za-z0-9_-]`, the
form the route accepts. Deferral and references need a gateway model that honours them: the adapter
sends them as they are and does not check the model. A call to a deferred tool that no result
loaded is served like any offered tool. Proved by `tests/test_backend_gateway_rounds.py`.

`person` is a `TurnPerson` (frozen, always a person): `principal` (the verified `(iss, sub)`),
`role` (the app role) and `groups`, an async callable returning the person's groups. Proved by
`tests/test_backend_port.py`: `test_a_turn_person_is_not_constructible_without_a_principal`.

A turn ends short of its answer with one exception, `TurnStopped(kind, code, retry_after_s,
detail)`: `refused` carries a `RejectionCode`, `failed` a `FailureCode`. A cancel reaches the
caller as `CancelledError`. The library never re-runs a turn: a re-run is a new turn that the app
or the person starts, and what the stopped turn already yielded stays shown. A host that re-runs
one by itself knows what its own tools did, and what it did from the events it was given, which
the library cannot know.

`TurnStopped.outcome` is what the stop means to a person, a `StopOutcome` derived from the code and
never stored beside it:

| Code | Outcome | Whether a retry can help |
| --- | --- | --- |
| `refused: not_enabled`, `refused: forbidden` | `not_enabled` | no, until the person's access changes |
| `refused: busy` | `busy` | yes, after `retry_after_s`, or after the app's own fallback when the gateway gave none |
| `refused: budget_spent` | `budget_spent` | no, until the budget changes |
| every `failed` code | `unavailable` | maybe, later, by the person |

`retry_after_s` comes only with `busy`, as the gateway gave it, on `TurnStopped` and on the voice
loop's `DecisionFailed` alike; the outcome never changes it. Proved
by `tests/test_backend_port.py`: `test_every_stop_code_has_exactly_its_outcome`,
`test_the_outcome_table_covers_every_stop_code`,
`test_a_turn_stop_and_a_failed_decision_of_one_code_give_the_same_outcome`,
`test_a_busy_refusal_is_busy_with_or_without_a_wait` and
`test_an_outcome_is_never_given_apart_from_its_code`.

Every release's wheel carries `agents/agents-vocabulary.json`, the values of `StopOutcome` in
declared order, which `scripts/generate_agents_vocabulary.py` writes from the enum. A kit that shows
outcomes pins the wheel by its release url and the sha256 digest GitHub records for the asset, and
reads that one member. Its `version` is the file's shape version: a changed token is a release, a
new set key keeps the version, only a changed shape raises it. CI fails when the file and the code
differ, and the release refuses a wheel whose member differs from the committed file.

`scripts/member-tool-wire.json` is the member-tool wire a person front declares: for each tool of
`MEMBER_TOOL_ARGUMENTS`, the schema of its arguments and of each answer record in
`MEMBER_TOOL_RESULTS`, refusals included, and under `resources` the schema of each record a member
resource carries, listed in `MEMBER_RESOURCE_RECORDS`. `scripts/generate_member_tool_wire.py` writes
it and CI fails when it and the code differ. Every release note ends with a `**Member-tool wire:**`
line that says `unchanged`, `fields added:` with the names of the resource records that only gained
optional fields, or `changed` with the names of the tools whose arguments or results differ and,
after `resources:`, of the resource records whose schema differs in anything else (followed by
`Fields added:` when other records only gained fields), from the previous release's snapshot
(`scripts/member_tool_wire_diff.py`), so a runner that serves an app reads it there. The guard
compares schemas: a rule across fields or a validator does not show in one, so its author names it.

`AssistantTextEvent` is a piece of the assistant's text, yielded as the gateway streams it, before
its round is judged. Pieces come from every round, including a round that ends in tool calls: its
pieces come before its `ToolCallEvent`s. `FinalEvent.text` is the last round's judged text only, so
when tools ran, the turn's pieces joined need not be `FinalEvent.text`; the last round's pieces
joined are. A turn that ends with `TurnStopped` may have yielded pieces first; nothing withdraws
them. An app that shows only judged text reads `FinalEvent`. Proved by
`tests/test_backend_gateway_stream.py`:
`test_each_text_piece_comes_as_its_own_event_in_order_then_the_joined_answer`,
`test_a_piece_reaches_the_app_while_its_round_is_still_open`,
`test_a_tool_rounds_pieces_come_before_its_calls_which_run_after_it_and_echo_whole` and
`test_a_round_that_streams_text_then_fails_leaves_the_text_shown_and_runs_nothing`.

`FinalEvent.usage` is a `TurnUsage` (`agents.turn`): its non-negative token counts and
`requests`, all summed over every round of the turn. `requests` is a required positive count of the
model requests the turn made: each round counts 1, so a turn with no tool round counts 1 and one
with two tool rounds counts 3. `input_tokens` is everything the model read, cached tokens included;
`cached_input_tokens` is the part read from the provider's cache, `cache_write_tokens` the part
written to it, `output_tokens` what the model wrote. `cache_write_1h_tokens` is the part of the
writes with the one-hour lifetime (default 0, never above `cache_write_tokens`; a `TurnUsage` built
with more is refused) and `cache_write_5m_tokens` the rest, which has the provider's default
lifetime of five minutes. A round's usage is the gateway's last usage
chunk: `prompt_tokens`, `prompt_tokens_details.cached_tokens` (absent: 0),
`prompt_tokens_details.cache_write_tokens` else `cache_creation_input_tokens` else 0, and
`completion_tokens`. The one-hour part is
`prompt_tokens_details.cache_creation_token_details.ephemeral_1h_input_tokens`; no split, or a null
lifetime, counts as none sent, and the total decides the rest: a split short of the total counts
the rest as five minutes, and a split above it keeps the total and the one-hour part as sent. A
one-hour part above the total (a split with no total included), negative or not an integer leaves
the round with no usage, as any other unreadable count does. The usage is read from a chunk with `choices: []`, with `choices` absent or
null, or alongside a choice; a chunk with neither `choices` nor a non-null `usage` fails the round as
a malformed reply. A chunk whose `usage` is null carries no usage: the round's usage is the last
non-null one, and a round with none has no usage. `usage` is `None`, never a partial sum, when
any round sent none or one with a missing, negative or non-integer count, and for a picture turn;
reading usage changes nothing else the turn yields. The library passes the gateway's counts through
unchecked: it does not verify that `cached_input_tokens` is no larger than `input_tokens`.

`FinalEvent.rounds` holds one `RoundUsage` (`agents.turn`) per round of a text turn, in order:
`started_at`, the backend's `now` read as an aware time just before the round's request opened,
held in UTC, and the round's own `usage` (`None` as above). `usage` is their sum, so a host reads the
last round's `input_tokens` and start from `rounds[-1]`. A naive time from `now` fails the turn
with a validation error, and a picture turn has no rounds. A turn that ends with `TurnStopped`
reports none, though its rounds were paid. Proved by
`tests/test_backend_streamed_round.py`:
`test_a_usage_chunk_with_no_choice_gives_the_rounds_usage`,
`test_usage_on_a_chunk_that_carries_a_choice_gives_the_rounds_usage`,
`test_a_usage_chunk_without_choices_gives_the_rounds_usage`,
`test_a_later_chunk_with_null_usage_keeps_the_usage_already_read`,
`test_an_event_that_is_no_chunk_is_malformed_with_no_context` and
`test_unreadable_usage_gives_a_round_with_no_usage_and_the_same_message` and
`test_a_turns_usage_refuses_a_missing_or_non_positive_request_count`,
`test_the_split_of_the_cache_writes_is_read_beside_their_total`,
`test_a_split_the_record_cannot_hold_gives_a_round_with_no_usage`,
`test_a_turns_usage_built_without_the_one_hour_part_has_every_write_at_five_minutes`,
`test_a_turns_usage_refuses_a_one_hour_part_above_its_total_of_writes` and
`test_two_usages_add_up_in_their_total_and_in_their_one_hour_part`; and by
`tests/test_backend_gateway_usage.py`: `test_the_final_event_carries_the_usage_the_gateway_sent`,
`test_a_usage_chunk_without_choices_gives_the_turn_its_usage`,
`test_the_final_events_usage_sums_the_usage_of_every_round`,
`test_the_final_events_usage_counts_one_request_per_round`,
`test_a_turn_whose_round_has_no_readable_usage_ends_with_no_usage_and_the_same_text`,
`test_a_turn_with_a_round_that_has_no_usage_ends_with_no_usage` and
`test_a_turn_that_stops_after_a_paid_round_yields_what_it_yields_without_usage`,
`test_each_round_is_reported_with_the_start_its_request_was_opened_at_and_its_own_usage`,
`test_a_round_without_usage_is_reported_as_such_and_leaves_the_turns_usage_unset`,
`test_a_clock_that_reads_a_naive_time_fails_the_turn_with_no_naive_start` and
`test_a_clock_in_another_zone_gives_the_start_in_utc`.

The app's configuration embeds `agents.config.AgentConfig` as the `agent` section:
`gateway` and `api_group`, none with a default; any other key, a `backend` one included, is refused.
`ChatGateway` is `origin` (scheme, host and optional port; no path, query, fragment or
userinfo), `key_file` and `ca_file`, both absolute paths. `ca_file` is required for HTTPS and
forbidden for HTTP. A missing or malformed value fails loud. Proved by
`tests/test_backend_config.py`: `test_a_missing_host_fact_fails_loud`, `test_a_malformed_section_fails_loud`,
`test_a_malformed_origin_fails_loud`, `test_a_malformed_file_setting_fails_loud`.

### What every app does the same

Every app names the same files: `agent/backend.py` builds the backend, `agent/tools.py` holds
its tools, `agent/mcp.py` its MCP surface, and `agent/settings.py` its `agent` configuration
section. `agent/settings.py` is the one place that imports `agents.config`.

`TurnPerson` is always the verified person, and `principal.iss` is passed exactly as the app
bound it: two spellings of one issuer split one person's budget. `busy` may carry no
`retry_after_s`, so the app needs a defined fallback; a value that varies by app stays the
app's. An app maps only `TurnStopped.outcome` to its wording and keeps `code` and `detail` for its
logs; a person over budget learns that the budget is spent, never an outage.

Tests use a fake `AgentBackend` that yields `TurnEvent`s and raises `TurnStopped`. A proof
through the real entry point runs `GatewayBackend` on `backend.fake_gateway.FakeGateway`, a
scripted transport passed as `transport=`.

## Agent files

An app declares its agents as Claude Code subagent files, `src/<pkg>/agents/<name>.md`, that
`agents.agent_file.load_agent` reads into an `AgentDefinition`: YAML front matter
(`name`, `description`, `tools`, `model`, `maxDurationSeconds`, `maxToolCalls`, `picture`,
`voice`, plus the extension `appRoles`, exactly `[admin]` or `[admin, user]`) and the Markdown
body as the prompt.

- `model` is a `text/<model>` name for a text agent (lower-case letters, digits, `.` and `-`)
  and `maxDurationSeconds` a positive integer; both are required. Proved by
  `tests/test_agent_file.py`: `test_a_file_without_a_required_key_is_refused_and_names_it`,
  `test_a_model_that_is_not_a_text_gateway_name_is_refused_and_named`.
- `maxToolCalls` is a positive integer, required exactly when `tools` is not empty and refused
  when it is. Proved by `test_a_missing_tool_call_bound_is_refused_when_there_are_tools` and
  `test_a_tool_call_bound_is_refused_when_there_are_no_tools` in the same file.
- `picture: {size: <width>x<height>}` (positive whole numbers, nothing else in the mapping)
  makes a picture agent: its `model` must be an `image/<model>` name, and it selects no `tools`
  and carries no `maxToolCalls`. Proved by
  `test_a_picture_file_that_does_not_fit_a_picture_agent_is_refused_and_named`.
- `voice: {windowBytes: <positive integer>}` (nothing else in the mapping) lets the agent run
  the voice loop and bounds what each of its decisions sends (see *The voice loop*);
  `AgentDefinition.voice_window_bytes` carries it, and a picture agent carries none, neither in
  its file nor in an `AgentDefinition` built by hand. Proved by
  `test_a_voice_agent_carries_its_window_and_any_other_agent_none`,
  `test_a_voice_section_that_is_not_one_positive_window_is_refused_and_named`, the picture
  test above and `test_a_definition_that_breaks_a_file_rule_is_refused_where_it_is_built`.
- A tool the file names must be declared, once. Proved by `test_undeclared_tool_is_refused_and_named`
  and `test_duplicate_tool_in_the_file_is_refused`.
- The file is read strictly: an unknown or duplicate key, malformed front matter, an unquoted
  type mismatch and a name that differs from the file's stem are refused with the file and the
  field named, never the value. Proved by `test_unknown_key_is_refused_and_named`,
  `test_duplicate_key_is_refused_and_named` and
  `test_formatted_traceback_does_not_show_file_values`.
- `offered_to(role)` is a display filter, not the security boundary. Proved by
  `test_offered_to_follows_the_app_roles`.

## The tool contract

`agents.tools.ToolDeclaration` is the one declaration of a tool: name,
description, arguments and a `ToolEffect`, `read_only` or `side_effecting`. The effect is for the
host (the person front reads it for the MCP read-only hint) and never reaches a prompt; it defaults
to side-effecting, because a tool nobody classified must be assumed to change state. Proved by
`test_the_request_carries_only_model_messages_user_stream_options_and_tools_without_the_effect` in
`tests/test_backend_gateway_tools.py`.

`HandTool` is a declaration with the async handler `(Principal, JsonObject) -> ToolOutcome` that
serves it in the app's own process; `ToolOutcome` is `content`, `is_error`, `image`, which is
`None` unless the tool lets the agent see, and `loads`, the deferred tools the answer loads (none
by default). An answer that loads is references alone: its `content` is blank and it has no error
and no image, and it names each tool once, only tools of the request's `deferred_tools`. The next
round's `tool` message holds one `tool_reference` part per name, in order, and the host gets the
names on `ToolResultEvent.loads`. An answer that breaks these rules is a host defect: the call
ends `failed`, the model reads `Error: The tool failed.` and the turn goes on. A `ToolImage` (`agents.turn`, exported by
`agents.tools`) is one JPEG still picture: its bytes, written as canonical standard base64 in JSON,
refused when empty or when they do not start with the JPEG start-of-image marker. JPEG is the one
media type a tool sends, and an outcome without an image is sent as before. Proved by
`tests/test_turn_tool_image.py`. The module exports `Principal` and `JsonObject` too, so an app's
`agent/tools.py` imports only `agents.tools`. `JsonObject` is a `dict[str, JsonValue]` (pydantic's
`JsonValue`), so a handler narrows a nested value with `isinstance` before it reads it. The module
imports neither `mcp_wire` nor anything of the MCP extra (`.importlinter` proves it).

## The gateway backend

`GatewayBackend(config, *, tools=(), transport=None, now=None)` runs a text turn, with the app's
`HandTool`s when the agent selects any, or a picture agent's turn. `now` is the clock that dates
each round's start: it returns an aware time, and the default is the system's UTC time. A tool name given twice
raises `ValueError` at construction; a selected tool with no handler, or whose request
declaration differs from its `HandTool`'s, raises `ValueError` at `run`, before any request.
Proved by `tests/test_backend_gateway_tools.py`: `test_two_tools_with_one_name_are_refused_at_construction`,
`test_a_selected_tool_without_a_handler_raises_before_any_request`,
`test_a_request_declaration_that_differs_from_its_handler_s_raises_before_any_request`.

The backend owns one pooled `httpx2` client, built on its first request and reused across
rounds, pictures and turns, so a later request skips the TCP and TLS handshake. The host closes
it: `async with GatewayBackend(...)` or `await backend.aclose()`; a `run` after that raises
`RuntimeError`. A pooled connection the gateway closed while idle is dropped before reuse and a
fresh one serves the turn; the client adds no retry, so a close that lands between that check
and the write fails the turn as `provider_unavailable: connection_failed`. After `[DONE]` a
round reads the rest of the reply, up to 1 second and 64 KiB, so the connection can be reused; a
longer or slower tail closes the connection instead and costs the turn at most that second. The
pooled client belongs to the event loop of its first request, so a host drives one backend from
one loop (`asyncio.run` once) and not across several.

**Admission,** in order, before any request: a `person.role` outside `request.roles` is
`refused: forbidden`; `person.groups()` runs once and a refusal it raises reaches the app
unchanged; an `api_group` the person lacks is `refused: not_enabled`. Proved by
`tests/test_backend_gateway.py`: `test_a_role_outside_the_agents_roles_is_forbidden_and_sends_nothing`,
`test_a_group_the_person_lacks_is_not_enabled_and_sends_nothing`,
`test_a_refusal_the_groups_lookup_raises_reaches_the_app_unchanged`,
`test_groups_are_asked_once_per_turn`.

**The key** is read per turn after admission: one printable ASCII line of at most 1024 bytes,
opaque whatever its prefix; any other content is `failed: provider_unavailable`. Proved by
`test_an_unusable_key_file_is_provider_unavailable_and_sends_nothing` and
`test_a_key_with_any_prefix_is_sent_as_it_is`. No log record holds it
(`test_debug_logging_writes_the_key_into_no_record`). However the turn stops, the key and the
reply's text are in none of the frames the gateway path leaves in the `TurnStopped`'s traceback. A
stop the gateway path raises leaves `run` as a fresh exception, raised from a frame holding
neither, with no underlying error in its `__context__` or `__cause__`
(`test_a_stopped_turn_holds_neither_the_key_nor_the_reply_in_any_frame_or_chained_error`). A stop
the host raises, from a handler or the groups lookup, stays the same exception, with the gateway
path's frames (the gateway's own and the tool call's, which holds the model's arguments) cut from its
traceback
(`test_a_handlers_stop_after_a_tool_round_comes_back_the_same_and_holds_no_key_reply_or_arguments`).

**The deadline.** `request.max_duration_seconds` is one deadline around the whole turn, every
round and handler included; past it the turn `failed: limit_exceeded` and a running handler is
cancelled. A stream that stalls ends at the deadline, and an app that pulls the next event after it
gets `failed: limit_exceeded` with no further read. Proved by `tests/test_backend_gateway.py`:
`test_a_transport_that_never_answers_ends_at_the_limit`; in `tests/test_backend_gateway_tools.py`,
`test_a_deadline_during_a_handler_cancels_it_and_ends_the_turn_at_the_limit`; and in
`tests/test_backend_gateway_stream.py`,
`test_a_gateway_that_stalls_after_a_piece_ends_the_turn_at_the_deadline` and
`test_an_app_that_pulls_after_the_deadline_gets_limit_exceeded_and_nothing_more_is_read`.

What the client sends for a text turn, and nothing else:

| Part | Value |
| --- | --- |
| Request | `POST <origin>/v1/chat/completions`, no redirect followed, no cookie stored or sent (the client's jar allows no domain), no proxy variable read; HTTPS trusts `ca_file` alone |
| Headers | `Authorization: Bearer <key>`, `Content-Type: application/json`, and the transport's own defaults; no `x-litellm-*` or `anthropic-*` header |
| Body | exactly `model` (the request's `text/<model>` name), `messages` (the system prompt, then the turn's messages, then the round's calls and results), `user`, `stream` (`true`), `stream_options` (`{"include_usage": true}`, on every round) and, when the agent has tools, `tools` (OpenAI function tools of each declaration's `name`, `description` and `parameters`, and `"defer_loading": true` on each deferred tool); it does not depend on `one_call_per_reply`; no `parallel_tool_calls`, `max_tokens`, `metadata`, `tags`, `fallbacks`, `num_retries`, `mock_response`, `api_key` or cache marker |
| `user` | `"fp-" + sha256(json.dumps([iss, sub], ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()`, 67 characters |

Proved by `tests/test_backend_gateway.py`: `test_the_request_is_one_post_with_the_system_prompt_and_the_messages`,
`test_no_header_beyond_the_transports_own_defaults_is_sent`, `test_no_pinned_absent_field_is_sent`,
`test_the_proxy_environment_is_ignored`
and `test_https_is_trusted_through_the_configured_ca_file_alone`; and by `tests/test_backend_gateway_pooling.py`:
`test_a_redirect_is_not_followed_and_a_cookie_is_not_kept`,
`test_a_cookie_the_gateway_sets_is_not_sent_back_over_a_pooled_connection`,
`test_consecutive_turns_share_one_connection`,
`test_a_connection_the_gateway_closed_while_idle_still_serves_the_next_turn` and
`test_closing_the_backend_ends_its_connection_and_refuses_further_turns`.

The same values in the same order give the same bytes, and within a turn a round's body begins
with the round before: its `model`, its `tools` and its messages up to that round's last message
are byte-identical. The library sorts nothing, so a host that wants the provider's cache to hit
across turns keeps its tool order and its schemas' key order. Proved by
`tests/test_backend_gateway_usage.py`:
`test_a_second_rounds_body_begins_with_the_first_rounds_model_tools_and_messages`.

The fingerprint's prefix, input and algorithm are one contract across apps: a change splits
every person's budget, so every caller computes it with
`agents.turn.person_fingerprint`. Proved by
`test_the_public_fingerprint_and_the_gateway_user_give_the_documented_vectors`,
`test_a_different_principal_gets_a_different_fingerprint` and
`test_a_fingerprint_is_67_characters_and_the_same_for_one_principal`.

| The gateway answers | The turn ends |
| --- | --- |
| 200, an event stream that ends with `[DONE]` after `finish_reason: stop`, non-empty text of at most `MAX_EVENT_BYTES` | one `AssistantTextEvent` per text piece as it arrives, then `FinalEvent` |
| 200, an event stream that ends with `[DONE]` after `finish_reason: tool_calls`, whole round | the calls run in order (see below), then the next request |
| 200 otherwise (a stream that ends or breaks before `[DONE]`, a `[DONE]` with no finish reason, an event that is no JSON object or no chunk, a line that is no UTF-8, empty text, another `finish_reason`, a lone surrogate, a longer reply, a round that is not whole) | `failed: provider_error`, detail `malformed_reply`; pieces already yielded stand |
| 200, an event stream with an error event (a JSON object with an `error` key), with or without a following `[DONE]` | `failed: provider_unavailable`, detail `gateway_failed`; nothing of the event is kept, its message included |
| 429 | `refused: busy`; `retry_after_s` from the `Retry-After` seconds, at most `MAX_RETRY_AFTER_S`; an HTTP-date or no header gives none |
| 422 whose error `type` is `budget_exceeded` (a spent person or app budget) | `refused: budget_spent`; decided from the status and the checked error type, never the message |
| another 4xx | `failed: provider_unavailable`, detail `gateway_refused`; a 400 or another 4xx reads no body, a 422 reads it within `MAX_EVENT_BYTES` only to find a spent budget |
| 1xx, a 2xx other than 200, 3xx or 5xx (a `Retry-After` is ignored) | `failed: provider_unavailable`, detail `gateway_failed`; no body is read |
| connection refused or reset, or lost while the stream runs, TLS failure, an origin the HTTP library refuses | `failed: provider_unavailable`, detail `connection_failed` |

Proved by `tests/test_backend_gateway.py`: `test_a_text_turn_ends_with_the_answer`,
`test_a_200_that_is_no_complete_text_is_provider_error`,
`test_busy_carries_the_standard_header_seconds_capped_or_none`,
`test_a_refusal_status_is_provider_unavailable_as_gateway_refused`,
`test_a_server_failure_is_provider_unavailable_and_ignores_retry_after`,
`test_a_lost_connection_is_provider_unavailable` and
`test_an_origin_the_http_library_refuses_is_provider_unavailable`; and by `tests/test_backend_gateway_stream.py`:
`test_a_round_that_streams_text_then_fails_leaves_the_text_shown_and_runs_nothing`,
`test_an_error_event_ends_the_turn_as_gateway_failed`,
`test_a_connection_lost_while_the_stream_runs_is_connection_failed` and
`test_a_stopped_turn_holds_no_text_from_the_wire`. A round is complete only at `[DONE]` after
`stop` or `tool_calls`; the client judges what the gateway sends, so a provider stream the
gateway cuts and closes with a finish reason of its own reads as complete. A person or app over budget reads as
`budget_spent`, no outage. `detail` is the reason of the request's line
(`tests/test_backend_gateway_refusal.py` proves `budget_spent`).

### The tool loop

The loop runs in the client, in process, over streamed rounds, each read whole before it is
judged. A round is judged whole before anything runs; its text shows as it streams. Only the
request's tool names are offered and accepted, so a call to any other name, arguments that are
not a JSON object, an empty round, a reply that names one call id twice or a call id used in an
earlier round refuses the round
(`failed: provider_error`) with no handler run. A round that would take the turn past
`max_tool_calls` ends `failed: limit_exceeded` the same way. Proved by
`tests/test_backend_gateway_tools.py`: `test_a_round_that_is_not_whole_runs_nothing_and_shows_nothing`,
`test_a_call_to_a_tool_the_agent_did_not_select_is_refused`,
`test_a_call_id_reused_in_a_later_round_refuses_that_round_after_the_first_call_ran` and
`test_crossing_max_tool_calls_fails_with_limit_exceeded`.

With `one_call_per_reply` on, a reply that carries several calls keeps all of them in its
message (ids are checked for doubles across all of them, in the round's judgement) and serves
only the first. Each other call gets an error result, `not run: one call per reply`, in the
model's messages only, after the served call's result and in reply order; the host sees no event
for it and it does not count against `max_tool_calls`. The model decides again and the turn goes
on. The backend alone holds the rule: the body never asks the provider for it, and the round's
`model_request.ended` line carries `calls_not_run`. Proved by
`tests/test_backend_gateway_one_call.py`.

Each round is rendered once, by the same function for the turn and for a replay: an assistant
message whose `content` is the round's text or `null` and whose `tool_calls` carry each call's
arguments as the text the model sent, then one `tool` message per call in reply order. A loading
answer is the `tool_reference` parts alone.

For each call, in order: `ToolCallEvent`; `await handler(principal, arguments)`; `ToolResultEvent`.
Proved by `test_two_calls_in_one_round_run_in_order_each_between_its_events`. A call, or the next
request, that the deadline has already passed when its turn comes is not started:
`failed: limit_exceeded`
(`test_a_call_event_taken_past_the_deadline_starts_no_handler_and_leaves_no_record`,
`test_a_deadline_that_passed_while_the_app_took_the_result_event_sends_no_next_request`).

A `ToolOutcome(is_error=True)` goes to the model, which reads it as `Error: ` followed by the
content, and the turn goes on (`test_an_error_outcome_reaches_the_model_and_the_turn_goes_on`,
`test_the_model_reads_an_error_outcome_differently_from_a_success_of_the_same_text`). A handler
that raises is a host defect: the client tells the model the call failed and writes its class name
only (`test_a_raising_handler_is_a_failed_call_naming_its_class_only_and_the_model_gets_an_error`).
The one exception is `TurnStopped`: it ends the turn unchanged, with no result event and no
further round (`test_a_handler_that_refuses_the_turn_ends_it_with_that_exception_and_no_further_round`). Task
cancellation reaches the caller unchanged and no later round runs
(`test_cancelling_between_a_call_and_its_result_reaches_the_caller_unchanged`). Per started call
one `tool_call.ended` line is written, as the section "The log" below says. A call
that started may have written, and a cancelled `asyncio.to_thread` write may still finish;
nothing implies it was undone. A blocking tool stays the app's: its handler uses
`asyncio.to_thread`.

A result with an `image` reaches the model in its own role-`tool` message, whose `content` is then
a list: an `image_url` part whose URL is `data:image/jpeg;base64,` followed by the image, then a
`text` part with the text (after `Error: ` for an error) unless the text is blank. Every later
round of the turn sends that message again, image included, so the model can read the picture after
its first answer too; each such round's `input_tokens` count it, since the gateway reports no
separate count. The `ToolResultEvent` carries the same `image`. Proved by
`test_an_image_result_reaches_the_model_as_an_image_part_inside_its_tool_message`,
`test_an_image_with_no_text_is_sent_as_the_image_part_alone`,
`test_an_error_outcome_with_an_image_sends_the_error_text_beside_it` and
`test_every_later_round_of_the_turn_resends_the_image`.

A tool's `content` and `image` reach the model, and so the gateway and the provider the model
names: a tool returns only what that may see.

| Bound | Value | Over it |
| --- | --- | --- |
| One tool result, its text's UTF-8 bytes and its image's bytes together | `MAX_EVENT_BYTES` | the model gets a fixed `is_error` sentence instead of the content and the image; the call stays run; the turn continues |
| One request of a turn, serialised, images as base64 | 4 × `MAX_EVENT_BYTES` | no request is sent; `failed: limit_exceeded` |
| One line, one event's data | 6 × `MAX_EVENT_BYTES` + 64 KiB | `failed: provider_error` |
| One round's text | `MAX_EVENT_BYTES`, judged per piece | `failed: provider_error`; the crossing piece is not shown |
| One assembled round | the request bound | `failed: provider_error`; no handler runs |

Proved by `test_a_tool_result_over_the_event_bound_is_replaced_and_the_call_still_ran`,
`test_repeated_large_results_stop_the_turn_before_a_request_over_the_bound`,
`test_an_image_result_over_the_event_bound_is_replaced_and_the_call_still_ran`,
`test_two_full_size_images_and_a_third_stop_the_turn_before_a_request_over_the_bound`,
`test_a_line_that_never_ends_is_cut_at_the_bound_as_provider_error` and
`test_a_round_past_the_request_bound_runs_no_handler`; the crossing piece by
`test_a_round_that_streams_text_then_fails_leaves_the_text_shown_and_runs_nothing`; the reader's
own bounds by `tests/test_backend_streamed_round.py`.

### A picture agent

A request with a `picture_size` runs after the same admission and under the same one deadline,
as one call: `POST <origin>/v1/images/generations` with a text turn's headers, client and
request bound. The body is exactly `model` (the request's `image/<model>` name), `prompt` (the
agent's prompt, a blank line, the message), `size` and `user` (the fingerprint above, so one
person has one budget in text and pictures); `n` and `response_format` are never sent. A picture
request holds exactly one user message and no tools, refused when the `TurnRequest` is built.
Proved by `tests/test_backend_gateway_pictures.py`: `test_the_call_is_one_post_to_the_images_path_with_the_headers_of_a_text_turn`,
`test_the_body_holds_exactly_model_prompt_size_and_user` and
`test_the_user_is_the_fingerprint_a_text_turn_of_the_same_person_sends`.

The reply holds exactly one `data` entry with `b64_json`. A non-null `url` is refused and never
fetched; `url: null` beside `b64_json` passes. The turn ends with
`FinalEvent(text="", files=(RunFile(name="picture", content=<the bytes>),))` and no
`AssistantTextEvent`. No media type travels: `RunFile` holds none, and the app sniffs the bytes.
Proved by `test_one_picture_ends_the_turn_as_a_file_with_no_other_event`,
`test_a_null_url_beside_the_picture_passes`, `test_a_non_null_url_is_never_fetched` and
`test_a_reply_that_is_not_exactly_one_canonical_picture_is_a_provider_error`.

| Bound or reply | Value | Over it or otherwise |
| --- | --- | --- |
| The HTTP body | `base64_length(MAX_RUN_FILES_BYTES)` plus `MAX_EVENT_BYTES` for the JSON around it | `failed: provider_error`, as a text body over its bound |
| The decoded picture | canonical base64, at most `MAX_RUN_FILES_BYTES` | `failed: provider_error` |
| 429, another status, a connection or read failure, the deadline | | as for a text turn, with the line `picture_request.ended` |

Proved by `test_a_body_one_byte_over_the_read_bound_is_a_provider_error` and
`test_a_picture_one_byte_over_its_decoded_bound_is_a_provider_error`. A vendor's moderation
refusal is a 400 and so reads as `provider_unavailable`; an app shows that it did not create a
picture. Closing the stream cancels the HTTP call, and the vendor may still bill it. Which sizes
exist is the gateway's: the client checks the size's shape only.

An app sets an agent's model with `agent.request(messages, model=name)`; without `model` the
file's model runs. A name of the wrong kind raises `ValueError` before any request
(`test_request_refuses_an_override_of_the_wrong_kind` in `tests/test_agent_file.py`). The
gateway may later withdraw a name an app stored: that turn ends `failed: provider_unavailable`
and the client does not fall back to the file's model.

Every request to the gateway (completions, pictures) carries the host's
`traceparent` from `logline.outgoing_headers()`, and none outside a trace
(`test_a_request_inside_a_trace_carries_the_traces_id`,
`test_a_request_outside_a_trace_carries_no_traceparent`). The client sets no level on the `httpx2`
or `httpcore2` logger: `logline.configure` caps both at WARNING, so a host without it gets the
httpx2 INFO line with the gateway URL and httpcore's DEBUG response headers under its own root
level. The key stays out of the log even under DEBUG, because `httpcore2` prints a request by its
method only (`test_debug_logging_writes_the_key_into_no_record`).

### Model prices

`await backend.model_prices(model)` returns the `ModelPrices` (`agents.turn`) of a model, `model`
being the gateway's name for it (`text/<model>`): `context_window_tokens`, the input, output,
cache-read and five-minute cache-write prices per token (`input_per_token`, `output_per_token`,
`cache_read_per_token`, `cache_write_5m_per_token`), the one-hour cache-write price
(`cache_write_1h_per_token`, `None` where the model has none) and `steps`. A step is a
`PriceStep`: `above_prompt_tokens` and the five prices that hold above that many prompt tokens,
each `None` where the gateway names no price for the step. `steps` is a tuple ordered by threshold,
empty when the model has none. The record reports; a host's pure decision combines it with
`TurnUsage`, and no decision lives in the client. `ModelPrices` is not on the `AgentBackend` port.

The call is one `GET /v1/model/info` with the app's key over the pooled client, no query, no person
and no budget, under a deadline of 30 seconds, and it writes one `model_prices_request.ended` line
as a request of a turn does. It reads LiteLLM's answer in `backend/model_prices.py`, the one place of
its key names: the entries whose `model_name` is `model`, `max_input_tokens` and the input, output,
cache-read, five-minute and one-hour write prices of their `model_info`, and a step for each
`<price>_above_<N>k_tokens` key that holds a price (N thousand tokens; other tiers such as
`_batches` are not read). A model's answer has one price record or none:

| Case | Raises |
|---|---|
| the gateway lists no entry named `model` | `ModelPricesError`, reason `model_not_listed` |
| entries of that name that differ in any read value | `ModelPricesError`, reason `entries_differ` |
| a missing (null or 0) input, output, cache-read or five-minute write price | `ModelPricesError`, reason `base_price_missing` |
| a refusal, a failing gateway, a lost connection, an unusable key, the deadline | `TurnStopped`, as for a turn |
| an answer that is not the list of entries, or an entry without its window | `TurnStopped`, `failed: provider_error`, detail `malformed_reply` |

`ModelPricesError` is raised from a frame that holds neither the key nor the reply. In a host's
proof, `backend.fake_gateway.model_info(model_entry(name, prices))` is the scripted answer; a price
field named in `unpriced=` is written as the gateway writes a missing one. Proved by
`tests/test_backend_model_prices.py` and the `model prices` cases of
`tests/test_backend_gateway_request_lines.py`.

### The log

The library logs through standard-library loggers named under `agents` and logline's guide
(`overnightworks-logline`, one JSON line per finished step, no content): a host calls
`logline.configure` once at its entry point and gets the lines with the running trace id. Without
`configure` the same records pass through the host's own logging setup. The host pins
`overnightworks-logline` at one tag itself; the `backend` and `mcp-server` extras declare only the
version range. A field never holds a prompt, reply, arguments, key, a handler's message or a
generation; only ids and closed codes.

`tool_call.ended` closes every tool call, with `tool_name`, `duration_ms` and `outcome`; the
gateway client adds `tool_call_id`, the person front has none
(`test_a_valid_result_is_one_ok_line`, `test_a_tool_call_writes_one_ended_line_with_its_outcome_and_the_tool_name`):

| Case | `outcome`, `reason`, level |
|---|---|
| a valid result | `ok`, INFO |
| the handler returns `is_error` | `refused`, `tool_error`, INFO |
| the handler raises | `failed`, `error_type` the class name, WARNING, no traceback |
| the result is over `MAX_EVENT_BYTES` | `refused`, `result_too_large`, INFO |
| a handler's `TurnStopped` refusing the turn (client only) | `refused`, its code, INFO |
| a handler's `TurnStopped` failing the turn (client only) | `failed`, its code as `reason`, WARNING |
| the deadline, the host's close, a client cancelling | `cancelled`, INFO |

The person front writes single lines for what it holds back: `answer.withheld` (`method` from the
served methods, and `tool_name` for an offered tool), `listen_stream.cut`, `tool_offer.failed` and
`resource_lookup.failed` (`method` and `error_type`). The route writes `route_answer.failed`, a
WARNING, with `reason` (`no_status`, `incomplete`, `too_large`, `status`, `not_text`) and `status`
when the app sent one. The inner route request carries the outer trace id under a new span id.

Each request to the gateway writes one line when it ends: `model_request.ended` per round of a
turn, `picture_request.ended` and `model_prices_request.ended`. A line carries `duration_ms`,
`outcome` and the response's `status` once one came; `connect_ms`, the time to open a new
connection (TCP connect, and the TLS handshake where there is one), only on a request that opened
one, whether or not a response came, never on a reused connection or a failed connect; a round's line also carries `first_output_ms`
at its first text piece and `input_tokens`, `output_tokens`, `cached_input_tokens`,
`cache_write_tokens` and `cache_write_1h_tokens` when the gateway sent a usage; a round that asked
for calls under `one_call_per_reply` also carries `calls_not_run`, the number of its calls left
unserved (0 or more). The `reason` is the stop's detail, or its code
when it has none; a failed line is a WARNING, because the turn reports the stop itself
(`tests/test_backend_gateway_request_lines.py`):

| Case | `outcome`, `reason`, level |
|---|---|
| answered 200 and read whole | `ok`, INFO |
| 429 | `refused`, `busy`, INFO |
| 422 with the `budget_exceeded` error type | `refused`, `budget_spent`, INFO |
| another 4xx | `failed`, `gateway_refused`, with the `status` |
| 5xx, or an error event in a 200 stream | `failed`, `gateway_failed` |
| a reply that is no round or picture, or a round that is not whole (a name not offered, an empty call id, a call id named twice or used in an earlier round, a replayed round's included, or a call id or text that is no valid Unicode) | `failed`, `malformed_reply` |
| a connection refused or lost | `failed`, `connection_failed`, with no `status` unless one came before |
| the turn's deadline | `failed`, `limit_exceeded` |
| the host's close | `cancelled` |

The place store and the listen record write one `<noun>.ended` line per call of the
store, with `duration_ms` and `outcome`; a call that fails is `failed` with `error_type`, the class
of the failure. A failed call adds `cause_type`: the type of the innermost cause a `PlaceStoreError`
stands for, such as the lost connection or the refusal the store library met, the name
`listen_lapse.ended` carries as `last_error_type`.
No line carries a URI, a generation or content:

| Line | Fields beside the timer's |
|---|---|
| `place_publish.ended` | `signal_kind` (`resource` or `tools`) |
| `listen_acknowledgement.ended` | `uri_count`, `turned_count` |
| `listen_end.ended` | none; a failed end or one past its cap is `failed`, WARNING |
| `listening_lookup.ended`, `listeners_lookup.ended` | `principal_count` on the second |

The lease writes no timed line of its own: tandem's `lease_renewal.ended`, `lease_state_read.ended` and `lease_roster_read.ended` time each renew and read, each with `lease_family`, `outcome` and, on failure, `error_type`.

State changes and background runs write their own lines, carrying `sub` where they name a person:

| Line | When |
|---|---|
| `listening.opened` | once for each URI an acknowledgement turned to yes, written by the pod that took it |
| `listening.lapsed` | once for each URI a lapse turned to no, written by the pod that claimed the lapse |
| `member_lease.opened` | a renewal started a lease |
| `member_lease.lapsed` | a lapse is handed to `lease_ended`; it may repeat as the handover may |
| `listen_lapse.ended` | a lapse run that lapsed or failed a stream: `stream_count`, `turn_count`, `failed_count` and, after a failure, `last_error_type`; `failed` with `reason` `store_failed` or `lapse_failed`, WARNING. A run that found nothing writes no line |
| `listen_lapse.resumed` | the first run after failed ones, with `failed_run_count`; a streak of failed runs writes only its first |
| `place_subscription.renewed` | a later confirmation of the subscription, with `stream_count` |
| `place_subscription.failed` | the subscription failed for good, ERROR, `error_type` of its cause |
| `place_signal.refused` | a payload on the channel that is no address: `reason` `not_an_address` and `character_count`, INFO |
| `host_callback.failed` | `listening_changed` or `listening_resynced` raised: `callback` and `error_type`, ERROR |
| `lease_lapse_service.failed` | the lapse service failed, WARNING, `error_type`, the first of a streak; a traceback only for an unexpected exception |
| `lease_lapse_service.resumed` | the restarted service ran longer than the pause without failing, INFO, with `failed_run_count` |

## The person front

`agents.mcp_server.McpPersonFront(tools, *, resolve_person)` is the MCP endpoint of
an app for the agent of one signed-in person. It serves MCP revision 2026-07-28 on the official
SDK and nothing older: there is no `initialize` handshake and no legacy path. `server/discover`,
`tools/list` and `tools/call` are POST only and answered in `application/json`. It is an ASGI 3 app
the host registers as the exact route `MCP_ENDPOINT_PATH` (in `agents.mcp_wire`),
never a `Mount`, which redirects `/mcp` to `/mcp/`, and behind the middleware that admits the
caller: the host's gate answers every 401 and 403 before routing, so the front answers no challenge.
The host enters `async with front.running():` once in its own lifespan, the one thing the SDK's
session manager needs and, with `ResourceChanges`, the place store's subscription; a request
outside it raises `RuntimeError`. The front runs on `asyncio`:
the allowance timer of a listen uses the running loop, which `anyio` alone does not offer. Proved by
`tests/test_mcp_server_person.py`:
`test_an_exact_starlette_route_serves_discovery_list_and_call_without_a_redirect`,
`test_a_request_outside_running_fails_loud`.

`resolve_person` is a port, `Callable[[Scope], Principal]`, and the front's one seam to the
host's authentication: the client imports no authentication library. The front calls it first on
every request, before it reads the body. Its failure propagates to the host: no tool runs and the
front writes no answer, because a front reached without the gate's admission is a host defect. A
result that is not a `Principal` raises `TypeError` the same way. Proved by
`test_a_resolver_failure_reaches_the_host_before_the_origin_check_and_the_body` and
`test_a_resolver_result_that_is_not_a_principal_reaches_the_host_before_the_body`.

A tool is `PersonTool(declaration, handler)`; the handler is
`PersonToolHandler = Callable[[PersonRequest, JsonObject], Awaitable[ToolOutcome]]`, and
`PersonRequest(principal, scope)` carries the resolved `Principal` and the request's ASGI scope,
which holds the bearer, `Host` and client address a handler replays. A name given twice raises
`ValueError` at construction. Proved by `test_a_call_hands_the_handler_the_resolved_person_and_the_request_scope`,
`test_concurrent_calls_each_get_the_person_and_the_scope_of_their_own_request` and
`test_two_tools_of_one_name_are_refused_at_construction`. An outcome answers one `text` content
block with its `content` and, with an `image`, an `image` content block after it, `mimeType`
`image/jpeg` and the image as base64 `data`; a text that is blank or only whitespace beside an
image is left out and the `image` block answers alone. A result whose text's UTF-8 bytes and
image's bytes together pass `MAX_EVENT_BYTES` answers the one `isError` text block `the tool result was too
large`, as the gateway backend does. Proved by
`test_the_sdk_client_gets_an_image_result_as_an_image_block_after_its_text`,
`test_the_sdk_client_gets_an_image_alone_when_its_text_is_blank` and
`test_an_image_result_over_the_event_bound_is_replaced_by_a_fixed_error`.

Resources and subscriptions, the standard's way for an agent to learn what changed, are one
more port and one more object on the front: see [the standard path](#the-standard-path).

A thin ASGI shell stands before the SDK and keeps this order. A request with an `Origin` header is
403 (its clients are agents, which send none), anything but `POST` is 405 with `Allow: POST`, a
body over `MAX_EVENT_BYTES` is 413, and a body with a repeated member, `NaN`, `Infinity` or a
number that overflows is refused before dispatch with the JSON-RPC parse error. Every request that
is not explicitly revision 2026-07-28, an absent or older `MCP-Protocol-Version` and `initialize`
included, gets `-32022` naming the served version. Proved by
`test_a_request_carrying_an_origin_of_any_value_is_forbidden_and_runs_nothing`,
`test_anything_but_post_is_method_not_allowed_with_the_allowed_method_named`,
`test_a_body_one_byte_over_the_bound_is_too_large_and_runs_nothing`,
`test_a_body_with_a_repeated_member_or_a_non_json_number_is_refused_before_dispatch` and
`test_anything_but_a_request_at_the_served_revision_is_refused_naming_it`. `tools/list` offers every
tool, or with `offered_tools` only the tools of the moment ([the tools of the
moment](#the-standard-path)), in declaration order, because rights are the route's, each with
`annotations.readOnlyHint` true for a `READ_ONLY` tool and absent otherwise
(`test_tools_list_shows_every_tool_with_the_read_only_hint_only_on_a_read_only_one` and
`test_tools_list_answers_only_the_offered_tools_in_declaration_order_per_member`).
The SDK answers the rest of the protocol (`Mcp-Method` mismatch `-32020`, missing `_meta` `-32602`,
unknown method 404): `test_the_sdk_ladder_answers_a_malformed_request_with_its_standard_error`.

A handler that raises, or returns an outcome that cannot be written, answers a failed tool
result with one fixed text; its `tool_call.ended` line names the tool and the exception's class,
and nothing of its message reaches the wire or the log
(`test_two_tools_failing_differently_answer_one_failed_result_without_their_messages`,
`test_an_outcome_that_cannot_be_written_answers_the_failed_result_and_leaks_nothing`). An answer
that carries the signature of the request's own bearer is replaced by an empty 500
(`test_a_result_carrying_the_request_bearer_is_withheld_and_the_log_names_no_caller_text`).

The front sets no level on any logger: the SDK's server loggers follow the host's logging setup
(`logline.configure` caps the `mcp` tree at WARNING), and
`test_running_leaves_the_level_of_every_sdk_logger_as_the_host_set_it` proves it. The front's own logger names only
the tool and the exception's class
(`test_no_credential_marker_reaches_any_log_under_debug_or_normal_logging`).

## The member module

`agents.member` exports what an app and an agent say to each other about a member:
`PlaceRef` (defined in `agents.member_place`, with `STANDING_URI` and the four URI functions, and re-exported here), `StandingState`, `Invitation`, `Standing` and its `ParentPlace(app, place)`, `MemberStanding(standings)` (what
`STANDING_URI`, `member://standing`, answers with), `PlaceWindow(generation, starts_at, entries, talked_over, interrupted, voice_state)`
of `PlaceEntry(seq, kind, name, text, own, spoken, cut, act, continues, language, traceparent)`, `LEASE_SECONDS` (120),
`INVITATION_SECONDS` (120), `LISTEN_BOUND_SECONDS` (50),
`LISTEN_ALLOWANCE_SECONDS` (5), `AcceptInvitationArguments(place, invitation, epoch, looks, figure, voices)` for the tool
`ACCEPT_INVITATION_TOOL`, `DeclineInvitationArguments(place, invitation, epoch, reason)` for
`DECLINE_INVITATION_TOOL`, `SetTurnStateArguments(place, stopped, thinking)` for `SET_TURN_STATE_TOOL`,
`GestureArguments(place, gesture)` for `GESTURE_TOOL` with its answer `Relayed` and its refusal
`GestureRefused`, `gesture_tool()` (the finished declaration an app offers while the shown look plays gestures, whose
schema's enum is figure's gestures), `SetFramingArguments(place, framing)` for `SET_FRAMING_TOOL` with its refusal
`SetFramingRefused` (its success is no record, `{}`), `framing_tool()` (the declaration an app offers while the shown
look plays framings, whose schema's enum is figure's framings), `SpeakArguments(place, text, language, style, mood, interrupted)`,
and the records of the tools `SPEAK_TOOL`, `STOP_SPEAKING_TOOL` and `CONTINUE_SPEAKING_TOOL`
(`StopSpeakingArguments(place, interrupted)`, `DroppedSpeaking`, `StopSpeakingAnswer`,
`ContinueSpeakingArguments(place, interrupted)`, `ContinuedSpeaking`, `ContinueSpeakingRefused`
with its `ContinueSpeakingRefusal`), and of being talked over and held (`TalkedOver`,
`Interrupted`) ([hearing and speaking](#hearing-and-speaking)). `spoken` is true for a line said aloud and
heard, false for a written one; every dumped entry carries it, and an entry without it reads as
written. `cut` is true only on a spoken line whose sentence was cut before its end, and a line that
is not cut dumps without the key. `act` is true only on a written line that says what someone did
rather than what someone said, and a line that is no act dumps without the key; a spoken line is
refused with `act`, so a cut line is too. `continues` is true only on a spoken
line whose speaker's next sentence at the place already waits to play as the line enters, and a line
that does not continue dumps without the key; a written line is refused with `continues`, and so is
a cut line; the library decides nothing on it. `language` is the ISO 639-1 code a spoken line was
heard in; only a spoken line carries it, absent means the app names none, and a line without it
dumps without the key; a written line is refused with `language`. Proved by
`tests/test_member_line_language.py`. An act
line is told and never calls the member to act. At a place with a view, `awaits_you` calls the
member to act. Whether a line that is no act wakes the member stays the runner's wake rule. A
window's `seq` ascends strictly and may skip; a non-empty window starts at its first `seq`; an
empty window is valid and its `starts_at` is the `seq` the next line gets. A place's window is the resource `place_window_uri(place)`,
`member://standing/places/{kind}/{id}` with each segment percent-encoded, so `/` in an opaque
`kind` or `id` stays inside its segment;
`place_ref_from_window_uri(uri)` returns the `PlaceRef` for that canonical spelling only and
`None` for anything else. A URI is no credential.

**How the member wire grows.** The member records are strict: building one with an unknown field
fails, an app refuses an unknown argument key from every caller, and a runner an unknown key in an
answer. A runner reads a standing, window or view through `member_resource_from_json`, which
ignores a key its release does not know, at any depth, and checks every field, enum value and rule
across fields it knows as before; a view's `state` is the app's document and keeps every key. A
release adds a field to a resource record without the runner taking it first only when the field
is optional, left out of the JSON while unset, and means nothing a runner that ignores it acts
wrongly on; a field that restricts what the member may do or changes the meaning of a field the
runner knows (as `act` does for a line's text) is a change. A union of two or more records inside a
resource record is told apart by a required tag, never by which keys are present, and a new
variant is a change. A runner on a release before the function, or one that parses with the
default, refuses every key it does not know, so until the deployed runner reads through the
function a change and an added field alike wait for the runner's release.
Proved by `tests/test_member_wire_evolution.py`, `tests/test_mcp_server_resources.py` and
`tests/test_member_tool_wire_diff.py`.

**A trace on a line and on a view.** `PlaceEntry` and `PlaceView` carry an optional `traceparent`,
a W3C trace context the app set when the line entered or the view's `revision` moved: the app writes
`logline.current_traceparent()`, which is `None` outside a trace, and a record without one dumps
without the key. The only check here is a length of 256 characters at most, so a value that is no
trace context is carried, and `logline.unit_trace` is the one reader that starts a new trace for it.
The runner opens the turn a line or revision woke under `unit_trace` of that value, so the turn
runs in the trace of the change that woke it; the voice loop does so for each decision, under the
newest line that woke it, and the hearing front's `heard` writes the line's value from the trace the
utterance was heard in. Proved by
`tests/test_mcp_server_hearing.py`: `test_the_heard_line_carries_the_trace_the_utterance_was_heard_in`,
and `tests/test_voice_trace.py`: `test_each_answer_runs_in_the_trace_of_the_line_that_woke_it`.

**A place's view.** Every place serves the member a window. A place whose state is no
conversation also serves a `PlaceView(revision, awaits_you, state, traceparent)` beside it: the window carries
what happened there, in lines, and the view the place's newest state. `state` is the app's JSON
object, carried untouched and never read by the library (a value that is no JSON value is
refused); `revision`, an integer from 0, changes whenever the place moves on for this member, that
is whenever what the member may do or must act on changes, while parts of `state` that say only who
is present may change without it; `awaits_you` says the place waits for this member to act, and the
app decides it from the state at that revision, so one revision has one `awaits_you`. The view is
the resource `place_view_uri(place)`, the place's window URI with the segment `view` after it;
`place_ref_from_view_uri(uri)` returns the `PlaceRef` for that canonical spelling only and `None`
for anything else. A `Standing` carries `serves_view`, false unless set and then left out of its
JSON, so a standing that does not set it dumps without the key. The listing rule: an app that sets
`serves_view` on a standing lists that place's view URI beside its window URI among the member's
current resources; otherwise no pulse for the view is delivered. A view listed without its
place's window is a host defect: the front answers that listing as it answers a port that raised,
at `resources/list` and at a listen's acknowledgement alike, and ends an open stream at the pulse
that finds it, logging only the type of its private exception.

**What happened.** An app with a view says in its window what happened at the place: each act
that moves the place on, by a person, by the member or by the app itself, becomes a line. A line
that says what someone did rather than what someone said is written (`spoken` false) and carries
`act`: its `name` is the one who acted, or the place when nobody did, its `text` the act in the
app's words, its `kind` the app's word for that sort of act, and `own` marks the member's own act.
A line shows the member no more than its view may show it. A change's lines are readable no later
than the revision they lead to; a change that adds lines and moves the revision signals both URIs,
a change of the revision alone signals the view, and a line alone the window. A runner reads the
view, then the window, so the window may already hold lines past the view's revision. It tells each
line with its `kind` and `act`, and the per-line sentence of [the member module](#the-member-module)
holds. Proved by
`test_an_act_line_survives_the_wire_with_its_marker`,
`test_a_line_that_is_no_act_dumps_without_the_key` and
`test_a_spoken_line_is_refused_with_act` in `tests/test_member_act.py`, and by
`tests/test_mcp_server_resources.py`, `tests/test_mcp_server_listen_view.py` and
`tests/test_voice_loop.py`.

An accept may name the agent's looks, `looks` as a list of `FigureLook(kind, url, sha256, gestures, room)` and
`VideoLook(kind, name)`, first choice first; its 3D figure, `figure` as `FigureRef(url, sha256, gestures)`;
and the voices it would speak with, `voices` as a list of voice names, the persona's order of
preference with the first choice first; all are optional, and an accept without them dumps without
their keys. The record checks only their form: `url` is an absolute `https` address with a host, no
user part, query or fragment, no whitespace, backslash or other non-printable character, and none of
`%`, `"`, `<`, `>`, `^`, `` ` ``, `{`, `|` or `}` in the host; a port is a number from 0 to 65535 or
empty; `sha256` is 64 lowercase hexadecimal characters; `voices` is the
`Voices` type, 1 to 8 names, none twice (a host types its own voice list with it), each 1 to 64
ASCII letters of either case, digits, `.`, `_` or `-`, and `looks` is the `Looks` type (a host types
its own look list with it): at least one `Look`, told by `kind`, at most one `FigureLook` and no
`VideoLook` name twice, a name being a `LookName`, the form of a voice name. The app decides the
rest ([CALLING-41](calling.md)):

- It shows the first look of `looks` it can show; the library decides the order and the form,
  never what an app can show. A look it cannot show is skipped for the next, and with none the
  accept still stands. An accept with `figure` and no `looks` has the one figure look, and with
  both only `looks` is read; `figure` stays beside `looks` until every app reads `looks`. It offers
  `gesture_tool()` and `framing_tool()` and shows a `mood` only while the shown look plays them,
  which a figure look does and a video look does not ([CALLING-37](calling.md),
  [CALLING-38](calling.md), [CALLING-50](calling.md)). It keeps a framing per seat
  ([CALLING-51](calling.md)).
- It fetches a figure only from sources it allows, and serves only bytes whose sha256 matches. A
  video look's `name` is passed unchanged to its video service, which decides whether it offers
  that look.
- `gestures` maps a token of figure's base set (`FigureGesture`) to an `HttpsFileRef(url, sha256)`,
  the persona's own clip for that gesture on this figure only; it is left out of the JSON when
  empty, a gesture has at most one clip and a clip cannot be sent without a figure. `HttpsFileRef`
  owns the url and digest check that `FigureRef` shares, and the app treats a clip as it treats the
  figure.
- `room`, on a `FigureLook` only, is an `HttpsFileRef(url, sha256)`, the agent's own room, left out
  of the JSON when unset; `FigureRef`, so the old `figure`, has none. The app treats it as it treats
  the figure and hands its bytes to figure's `room` mount option, whose room check judges them; a
  well-formed room that is refused shows none and the accept stands; a malformed one refuses the
  accept's arguments ([CALLING-52](calling.md)).
- It passes the voices to its speech service unchanged and in order; the library folds no case and
  picks no voice, and the speech service picks the voice that speaks each sentence. A name that
  service does not offer is refused there, and the app answers with the refusal it already has; the
  library adds no refusal member for it.
- An absent field means the app's own default.

A `Standing` is a `PlaceRef(kind, id)`, opaque to all but the app, a `StandingState` (`invited`,
`active` or `paused`) and, exactly when invited, an `Invitation(id, epoch, expires_at)`; any
other combination is refused at construction. `epoch` is the place's removal epoch, so the app
refuses an accept after a removal. A paused place is listed and a place the member left is
absent, so a pause is told from a removal.

A `Standing` carries `parent` as `ParentPlace(app, place)`, absent from its JSON unless set. It
names the place this one lies inside: `app` is the parent app's MCP resource URL, parsed as an
absolute http or https URL, never fetched, so two spellings of one address compare equal as parsed
URLs; `place` is a `PlaceRef`. A malformed parent fails the parse of the standing. An app sets it
from the invitation on ([calling.md](calling.md), CALLING-27 and CALLING-28). Proved by `tests/test_member.py`.

An `accept_invitation` refusal is an `is_error` result whose content is
`AcceptInvitationRefused(refused)` as JSON, in `speak`'s shape (`SpeakRefused`), with one
`AcceptInvitationRefusal` code: `stale_invitation` (the invitation is no longer standing, has
expired, or its id or epoch no longer matches, or the member holds no lease),
`two_seats` (the person who brought the member does not hold exactly one seat in the place, the
inviting one), `sponsor_away` (that person's seat lost its connection and waits to resume) or `busy`
(the place has no free seat). A code the app adds for every tool alike is its own and not part of
the record; a runner reading it gets a validation error and decides itself. Proved by
`tests/test_member.py`.

`decline_invitation` takes `DeclineInvitationArguments(place, invitation, epoch, reason)`: the same
place, invitation id and epoch as an accept, and the required `reason`, a `DeclineInvitationReason`
whose only value is `busy` (the runner is at its own limit of how many places it is in at once).
It needs no lease, as a decline seats nothing. A standing decline answers a result without
`is_error` whose content no record pins. A refusal is an `is_error` result whose
content is `DeclineInvitationRefused(refused)` as JSON with the one `DeclineInvitationRefusal` code
`stale_invitation` (the invitation is not that member's, no longer stands, has expired, or its id or
epoch no longer matches). A decline's `busy` is not an accept's `busy`: the first says the runner
is at its limit, the second that the place is full. An app that does not serve the tool answers an
unknown tool, and the runner lets the invitation run out ([calling.md](calling.md), CALLING-8); the
app's part is CALLING-29 to CALLING-31. Proved by `tests/test_member.py`.

`set_turn_state` takes `SetTurnStateArguments(place, stopped, thinking)`: the place, and `stopped`, a
`StopOutcome` of `agents.turn` that says why the member's turns there stop; an absent or null
`stopped` states that they answer, and a dump leaves it out. The value is a closed enum, never free
text. `thinking` is a bool, false when absent and left out of a dump when false: true states that
a turn is running at the seat, and the record refuses it together with `stopped`. Success is a
result without `is_error` whose content no record pins. A refusal is an `is_error` result whose
content is `SetTurnStateRefused(refused)` as JSON with the one `SetTurnStateRefusal` code `not_held`
(the caller holds no seat, active or paused, at the place). An app that does not serve the tool
answers an unknown tool, and the runner logs it and goes on. The rules for the runner and the app
are CALLING-33 to CALLING-36, CALLING-48 and CALLING-49 in [calling.md](calling.md). The outcome is
the coarse word a person sees; the fine code behind it is in the `turn_ended ... code=`
log line of the host's runner, where the operator finds it. Proved by `tests/test_member.py`.

**A malformed member-tool call.** The person front checks a call of a member tool against the tool's
arguments record before the handler runs, by the tool's name: `MEMBER_TOOL_ARGUMENTS` of `agents.member`
maps each member tool name to its record, and `member_arguments(model, arguments)` parses a call as JSON
(a `voices` list parses to its tuple) and raises a `ValidationError` when it does not fit. A call that
does not fit is an `is_error` result whose text is `the arguments do not fit the tool: ` followed by each
failing path in dots and its pydantic error type in parentheses, such as `figure.gestures.wave.url (value_error)`,
once each and sorted. A key that is a word of the vocabulary the record declares, such as a gesture, is written
out; a key the caller chose, such as a word outside that vocabulary or a field the record lacks, is written
`[key]`, and the text never carries an input value. The handler does not run, and the tool-call line is
`refused` with reason `tool_error`. It is the tool's own answer to malformed input; none of the refusal codes
above stands for it, so a host's handler never refuses a call that does not fit, and a host that types its
own arguments uses `member_arguments` instead of a copy. A call that fits and cannot be served stays the
host's refusal. A tool that is no member tool, such as one generated from a route, is not checked here.
Proved by `tests/test_mcp_server_member_arguments.py`.

How a person calls an agent to a place and sends it away is the same in every app:
[calling.md](calling.md).

## The calling module

`agents.calling` decides how an agent is called to a place and sent away, by the rules of
[calling.md](calling.md), on facts the app supplies. It is pure: it holds no state, reads no clock,
store, token or realm and performs no I/O, and it imports pydantic and agents' pure modules
(`member`, `turn`) only, which `.importlinter` holds, so an app's core may import it. The app
passes `now` and the invitation's id, keeps one `BookEntry(epoch, invitation)` per agent and place
in its own store, and writes the entry a decision returns in the same step as its seats. Every
binding is an opaque, non-empty text the app chooses, compared by equality only; an invitation is
open while `now < expires_at`. A missing row counts as epoch 0.

| Decision | Answers | Order and rule |
| --- | --- | --- |
| `invite(entry, CallFacts, invitation_id, now)` | `Invited(entry, invitation)` or an `InviteRefusal` | `not_present`, `not_a_member`, `already_there`, `not_listening`, `invited_by_another`; then an open invitation by the same inviter is returned unchanged; then `owner_absent`, `owner_away`, `owner_elsewhere`, `no_free_seat` (CALLING-1 to 6). The first that holds wins |
| `check_accept(entry, AcceptInvitationArguments, AcceptFacts, now)` | `Accepted(entry)` or `AcceptRefused(refusal, ended)` | `stale_invitation` (no open invitation, another id, epoch or owner, already seated, no lease) changes nothing; then `two_seats`, `sponsor_away`, `busy`, each ending the call (CALLING-7) |
| `check_decline(entry, DeclineInvitationArguments, now)` | `Ended` or `DeclineInvitationRefusal` | `stale_invitation` when no invitation is open or its id or epoch differs; otherwise the call ends busy (CALLING-30, 31) |
| `expired(entry, now)` | `Ended` or `None` | an invitation no longer open ends with `no_answer` at its `expires_at` (CALLING-8) |
| `owner_left(entry, now)` | `Ended` or `None` | an open invitation ends with `owner_away` at `now` (CALLING-6) |
| `send_away_allowed(...)` | `bool` | present, and a fresh member of the agent-adders group, the owner or holding a right the place's own rules give (CALLING-9) |
| `after_send_away(entry)` | `BookEntry` | the epoch raised by one and no invitation (CALLING-10) |
| `standing(entry, place, held, serves_view, parent, now)` | `Standing` or `None` | a held place is its state with no invitation, otherwise an open invitation is `invited`, otherwise none; `serves_view` and `parent` pass through (CALLING-27) |

`OwnerAt` is the app's reading of its presence rule: `AT_BINDING`, `ABSENT`, `AWAY` (its seat lost
its connection and waits to resume) or `ELSEWHERE` (present, but not on exactly that binding).
`Ended(entry, inviter, outcome, at)` tells the inviter, and only the inviter, how a call ended
without a seat (`CallOutcome`: `no_answer`, `busy`, `owner_away`); its entry keeps the epoch and
holds no invitation. `InviteRefusal` and `CallOutcome` are this module's own words: an app maps
them onto its own refusal tokens. How long an app keeps an `Ended` for its page is the app's.
Proved by `tests/test_calling.py`.

## The standard path

An agent learns what changes through the standard's resources and subscriptions, never by
polling a tool: the app serves `member://standing`, one window per place and, beside it, the view
of a place that has one, and tells the agent when one changed. This section is what a host wires
and what a runner may rely on.

**The host** passes `resources=` and `changes=` to `McpPersonFront` and enters
`async with front.running():` in its lifespan (see above). `resources` is one port,
`MemberResources` (exported by `agents.mcp_server`): `current(principal)` returns
every URI that principal may read now, and `read(principal, uri)` returns the app's current
`MemberStanding`, `PlaceWindow` or `PlaceView` for it, or `None` when it is not that principal's. The principal
is always the request's verified one and no argument names a recipient. `changes` is one
`ResourceChanges(store)` (exported by `agents.mcp_server`), without the port a `ValueError`:
the host awaits `changes.changed(recipient, uri)` for the recipient's `Principal` only after the
change is visible to a read, on the front's event loop or through `anyio.from_thread.run`; a
call for a principal with no open stream on any pod does nothing. `server/discover` advertises `resources`
with the port, `resources.subscribe` true with `changes`, and `tools.listChanged` true only with
`offered_tools`; `resources.listChanged` is always false.

**The place store.** `store` is a `PlaceStore` (exported by `agents.mcp_server`) shared by every
pod of the app; `RedisPlaceStore(redis, *, key_prefix)`, imported from
`agents.mcp_server.redis_place_store` and installed with the `redis` extra, is the implementation
of an app that runs more than one pod, and is the `LeaseRecord` of [the lease](#the-lease) too.
`InMemoryPlaceStore()`, exported by `agents.mcp_server`, is the one a host's tests give
`ResourceChanges`: every `ResourceChanges` handed the same instance is a pod of one app, it keeps
its signals, its listen record and its subscriptions in the object, and it reads time off the
running loop, so a virtual clock moves a stream's deadline. Built with
`confirms_subscription=False`, it confirms no subscription until `resubscribe()` is called, which
also confirms every subscription again as after a reconnect; `fail_subscriptions(failure)` ends them for good, and
`hold_signals()` until `release_signals()` keeps them from handling a signal or a turn. It imports
no store library. `changed` and
`tools_changed` publish an address, never content, and every pod's front, the publishing one
included, delivers it to its open streams, which read the app's own truth; a listening turn carries
its direction as well, and the store holds the listen record that
**Whether a member listens** below describes. Pass `RedisPlaceStore` your app's
`redis.asyncio` client, the `tandem.Client` of its parameter, and its key prefix: its repository
name, the one segment (`[a-z0-9][a-z0-9-]*`) your app gives its own tandem. agents keeps everything
it stores, tandem's channel included, under `<prefix>-agents`, so its keys and wakes never meet your
app's; it refuses a prefix ending in `-agents`. A member's lease keeps its keys beside the listen
record's, `<prefix>-agents:tandem:v1:epoch` and `<prefix>-agents:tandem:v1:l:member:*`, on tandem's
leases of family `member`, and one `<prefix>-agents:v1:member-facts:*` key per member, which expires
with its lease; the `redis` extra names tandem by range only (the README's extras section owns it) and the host pins the tag itself, as it
pins logline, because uv resolves one version.
The client carries a `Retry` and sets
`socket_timeout` and `socket_connect_timeout`: without the timeouts a publish to a silent Redis
waits for ever, and without the `Retry` the first re-read after a Redis restart can fail on a
stale pooled connection (tandem's README names both duties). The client is built without
`decode_responses=True`: on a decoding client redis-py drops the connection on a payload that is no
UTF-8, so each one costs a resync (an open gap on overnightworks/tandem#2). The first `changed` or
`tools_changed` after a Redis restart may raise `PlaceStoreError` once the `Retry` gives up; the
host expects that and relies on the listen's read within `LISTEN_BOUND_SECONDS`.

**The trace across a wake.** Every wake carries its sender's trace: `publish` sends the caller's
current trace and the Lua scripts of the listen record send `logline.current_traceparent()`.
tandem's `WakeChannel.following` runs each handler inside the trace of the wake's sender, so a
`listening_changed` of a host runs in the trace of the listen request that turned the answer on
another pod, and the signals and turns the port hands over carry no trace of their own. Proved
by `test_a_wake_is_handled_in_the_trace_of_the_pod_that_sent_it` and, on two pods,
`test_a_listen_on_one_pod_reaches_the_host_of_the_other_in_the_trace_of_its_request`.

Each lapse run of the listen poll runs in a trace of its own,
`logline.unit_trace(None)`, so its `listen_lapse.ended`, `listen_lapse.resumed` and
`listening.lapsed` lines and the `listening-no` wake it writes share one trace id, and a pod that
hears that wake handles it in it. A run that finds nothing writes no line. Proved by
`test_each_lapse_run_writes_its_lines_in_one_trace_of_its_own` and
`test_the_no_a_lapse_run_wakes_is_heard_in_the_trace_of_that_run`.

`running()` yields at the first confirmed subscription, so a pod turns ready only once it hears
changes. The front enters the store's `following(*, on_resync, on_signal, on_failed,
ready_within)` with `ready_within` ten seconds: a subscription the store refuses, or does not
confirm in that time (Redis unreachable, say), raises `PlaceStoreError` out of `running()`, which
fails the host's startup. A host that implements `PlaceStore` implements `following` as an async
context manager: entering returns after the first confirmation and `on_resync` returned, `on_resync`
runs at every confirmation, `on_signal` for each signal and turn after it, the handlers one at a
time in arrival order, and a subscription that cannot recover afterwards calls `on_failed` once
and leaves the context entered. At every
later confirmation each open stream receives every URI it honored, and the tool list if it
honored that, and the front writes `place_subscription.renewed`, because a change sent while the
subscription was down is lost. A publish is one send under the host's Redis client's `Retry`, so a change may arrive twice; each
handler carries a state (an address to re-read, never content), and a second arrival costs one
more read of it. When a publish fails, `changed` raises `PlaceStoreError` (a host never imports
tandem or redis to catch it) once the client's `Retry` gives up: delivery is uncertain, and a
member sees the change by its next listen's read within `LISTEN_BOUND_SECONDS`. A payload on agents' channel that is no address
writes `place_signal.refused` with its length and is skipped. If the subscription fails for good
after startup, the front writes `place_subscription.failed`, ends its open listens and answers
every request with 503 until the pod restarts.
Proved by `tests/test_mcp_server_place_wake.py`, `tests/test_mcp_server_memory_place_store.py`, `tests/integration/test_redis_place_store.py` and
the tests of `tests/integration/real_redis`, which `scripts/run_redis_tests.sh` runs.

**The answers.** `resources/list` answers exactly `current`, sorted (a view only beside its
window, see [a place's view](#the-member-module)), each entry with the URI as its
`name` and `application/json`. `resources/read` answers one text content with the requested URI,
`application/json` and the record's JSON. A `None` answers `-32602` with one fixed text that does
not echo the URI, so an unknown, a foreign and a non-canonical URI look alike. A port that raises
answers `-32603` with one fixed text and logs only the exception's type. Both carry `ttlMs: 0` and
`cacheScope: "private"`. `resources/templates/list` is not served. Proved by
`tests/test_mcp_server_resources.py`.

**The runner** listens with `resourceSubscriptions`, waits for the acknowledgement, which is the
first frame and holds the requested URIs in `current(principal)`, retains what arrives and only then
reads, so a change in a gap between streams is never lost. A requested URI that `current` does not
hold when the listen opens is not honoured on that stream, and the acknowledgement leaves it out: a
runner that listens on one place before its seat appears listens again once it does, while a listen
on `member://standing` also covers a window or view that appears later. A notification says only
that something changed: `MemberStanding` is the whole standing, so after a lost, repeated or late
one the next read shows today's truth. A subscriber of `member://standing` also receives the URI of
a place window or view `current` holds; a subscriber of one place gets that place alone. The stream
ends after `LISTEN_BOUND_SECONDS` with a completion result carrying the subscription id; a stream
cut without one is `SubscriptionLost` at the SDK's client. After either the runner listens again,
and reads again once acknowledged. A listen needs an SSE `Accept`, else it is `406`. The SDK's
`Client.listen` holds no task group, so whatever the runner's own loop body raises arrives bare at
its `yield`: a runner that maps the stream's failures maps only the stream's own, which are the
opening, `SubscriptionLost` during iteration and its deadline.

**Revocation.** Every pulse is checked against `current` when it is drained from the stream's
pending URIs, so a removal ends delivery on an open stream, and `current` runs once per pulse, so
it must be cheap. The check is not at the socket: after a removal, up to two already-checked
URI-only frames can still reach a client that stalled, so a host relies on the read for
revocation, not on the absence of a pulse. A port that raises at the acknowledgement answers
`-32603` with one fixed text, one that raises later ends the stream with the completion, and both
log only the exception's type. Proved by `tests/test_mcp_server_listen.py`.

**Whether a member listens.** `await changes.listens_to(principal, uri)` answers, on every pod
alike, whether that principal holds a stream, on any pod, whose acknowledgement honored the URI and
whose lapse has not yet run; `await changes.listeners(uri)` lists those principals. A stream lapses
`LISTEN_ALLOWANCE_SECONDS` after it ends, at the next lapse poll (about a second), or `claim_for`
later if the pod that claimed its lapse died. `PlaceStore.lapse_due` returns a `LapseRun` (the
`turns` it published, the `stream_count` it lapsed, a `failed_count` with the `last_error_type`),
which a host that implements the port builds, with `LapseRun`, `ListeningTurn`, `PlaceSignal`,
`ResourceChange` and `ToolsChange` imported from `agents.mcp_server`; `Principal`
comes from `agents.turn`, its one owner, and `agents.mcp_server` does not re-export it. The run's
pod writes `listening.lapsed` for each turn. Both read the place store on Redis time and raise
`PlaceStoreError` when it fails; a host that gets the error does not offer the agent. An app that
offers an agent only while it can answer an invitation asks for `STANDING_URI`. The allowance lets a
runner that listens again at once (the connection and one `current` call, a few milliseconds plus the
port's latency) keep the answer yes. The answer is no before the acknowledgement, after the allowance
of the last such stream and for a stream that honored only a place window, because the URI is
compared as asked and not widened to the places a standing subscriber also receives. Several streams
of one principal count while any of them counts. A stream that honored only `toolsListChanged`
touches no store.

The answer reads what the stream's acknowledgement honored; it does not re-check the member's
current resources, so a removal turns it no only once the stream has ended and the allowance has
passed. The standing is every admitted member's own resource, and whether the member still holds
its account and its places is the app's own record: an app combines the answer with that record.

`ResourceChanges(store, listening_changed=..., listening_resynced=...)` takes two optional
callables. `listening_changed(principal, uri, listens)` reports each turn of the answer for a
principal and URI. The pod whose acknowledgement turned it to yes calls it with `True` on that
request task, with request context; every pod, that one included, calls it with the turn's
direction on its signal task, without request context. On every pod the signal-task calls follow
the store's turns in order and miss none while the subscription stands; a resync covers a gap. The
`True` on the acknowledging request task is an early copy for request context and may come before an
older `False` on the signal task. A host's state follows the signal-task calls, a `True` without
request context included. `listening_changed` may repeat a direction, so a host applies it
idempotently: a `True` for a principal it already lists and a `False` for one it does not list
change nothing. The call is made for every URI the acknowledgement honored, so a host picks the
ones it offers on. `listening_resynced()` is called after every confirmed subscription, the first
one included, because a turn sent while the subscription was down is lost; the host rebuilds its
view from `listeners(uri)` and applies the `listening_changed` calls that arrive during the rebuild
after it, so a `False` is not overwritten by an older read. Neither callable may block (a host
schedules its own work from it), and an exception from either writes `host_callback.failed` by type
and never reaches a listen.

The record keeps each stream that honored a URI as a tandem deadline, so a stream lapses once its
deadline passes whichever pod opened it. A pod that dies leaves a stale yes for at most
`LISTEN_BOUND_SECONDS` plus `LISTEN_ALLOWANCE_SECONDS`, plus the lapse's latency; after a Redis
restart without persistence every answer is no until each runner's next listen, which is at most
`LISTEN_BOUND_SECONDS` away. When the store fails at a listen's acknowledgement the listen answers
`-32603` with one fixed text and calls nothing on the request task; the record may still hold the
stream, which then lapses after the allowance. Proved by `tests/test_mcp_server_listen_liveness.py`,
`tests/integration/test_redis_place_store.py` and the real-Redis tests of
`tests/integration/real_redis`.

**The tools of the moment.** A host that offers a member only the tools it can use now passes
`offered_tools=`, an `OfferedTools` (exported by `agents.mcp_server`): `(principal) ->
Awaitable[Collection[str]]`, the names of the declared tools that principal may use now. It needs
`changes`, else `ValueError`. The front reads it once per `tools/list` and once per `tools/call` of
a declared tool, never caches it, and never passes it a request argument, so it must be cheap.
`tools/list` answers the offered tools in declaration order with `ttlMs: 0` and `cacheScope:
"private"`. A call to a declared tool outside the offer runs nothing and answers a failed result
with the one text `the tool is not offered now`; a name never declared stays `-32602`. A port that
raises, names an undeclared tool or answers no collection of names (a bare text is none) answers
`-32603` with one fixed text and logs only the exception's type. When the offer changes, the host
awaits `changes.tools_changed(recipient)`, after the new offer is visible to the port and on the
same terms as `changed`. Every open stream of that principal that honored `toolsListChanged` then
receives one `notifications/tools/list_changed`, repeated changes before the notice is handed to
the stream arriving once; a change after that handoff may send one more notice, which a runner
answers by listing again. A stream writes its pending tools notice ahead of its pending resource
pulses, also one pending from an earlier change, after a resync and when it arrives again; the
resource pulses go out by first arrival, a repeated URI staying at its first place. A publish that
starts after another one's await has returned reaches the recipient's streams on every pod after
it, so a host whose change moves a member's offer awaits `tools_changed` to its end before it
starts the `changed` of the same change. Not ordered: publishes that are not run one after another
(two pods, two tasks at once), a resource pulse already taken for writing when the notice arrives,
and a notice lost to a `PlaceStoreError`. A runner that keeps its list therefore lists again when
a notice arrives during a turn and after a call answers `the tool is not offered now`; a stale
list costs one refused call and never an act outside the offer. A front with `offered_tools`
honors `toolsListChanged` when asked, and no front honors `promptsListChanged` or
`resourcesListChanged`. A runner that relies on the notice lists tools only after
such a stream's acknowledgement, as it reads resources, so a change between streams is never
lost; a runner that lists at every turn start needs no notice. The offer is not a right: the
marks and the host's gate stay the boundary, and a route refuses an act its moment does not
allow. A stream that honored only `toolsListChanged` counts for no URI in `listens_to`. Proved by
`tests/test_mcp_server_offered_tools.py`.

**The lease.** Every call a member makes is an admitted call, each opening of a stream included,
and the app renews the member's lease on each with `await leases.renew(principal, facts)`, where
`leases` is a `MemberLeases(store, lease_ended=...)` (exported by `agents.mcp_server`, `store` a
`LeaseRecord` such as `RedisPlaceStore`). `renew` is async, so an app calls it from an async site, an
ASGI wrapper around the front, never from the synchronous `resolve_person`. A runner that calls
again within `LEASE_SECONDS` keeps its lease. The lease says nothing about listening: a call from an
agent that listens nowhere renews it too. The listen is the offer rule only; a request to call an
agent and the agent's accept keep the lease as their check.

`renew` takes the member's `MemberFacts(owner, name)` (from `agents.member`): what the verified
token of the call states of its owner's subject and its own name. The store keeps them beside the
lease for `LEASE_SECONDS` from the latest renewal, so a renamed agent shows its new name from its
next call; an owner change replaces them and keeps the generation. Two pods renewing one member at
once may store their facts out of order, and the next renewal corrects them.
`await leases.facts(principals)` answers, in one read on every pod, the facts of each of those
principals that has some; a principal is left out when it never renewed, its lease lapsed, the
store lost its data, or its latest renewal raised, which may have kept the lease without its facts.
A stored value that cannot be read raises `PlaceStoreError` with a text that carries none of it,
and fails every batch read that includes its principal until that agent renews. An app reads the
facts for its roster and for the owner a person's call binds to, never logs them, and keeps no copy
of its own.

`renew` answers `LeaseRenewal(started, generation)`. The generation is a random text, new at every
start, and an app compares it by equality only: a renewal the store replays answers
`started=False`, so an app treats a generation other than the one it stored as a start. A generation
is app-internal: an app never shows it to a person and never logs it, because it is a value that can
end the lease. `await leases.state(principal)` answers `LeaseStanding(holds, generation)` from one
snapshot and renews nothing. `holds` is true while the lease runs and also right after a fresh
namespace or a Redis restart, for `LEASE_SECONDS`, when a lease may have been lost with the data;
`generation` is set only while the lease runs, so a `holds` without a generation is that unknown
state, and an add an app accepts then is caught only by its own reconcile. `await leases.live()`
answers `LeaseRoster(holders, settled)`: the principals whose lease runs, and whether `settled`, which
is true once `LEASE_SECONDS` have passed since the namespace began, so that a principal missing from
`holders` is gone and not lost. All four raise `PlaceStoreError` when the store fails.

`leases.running()` is entered once in the app's lifespan, on every pod. It hands each lapse to
`lease_ended(principal, generation)` on one live pod, at least once: the store's claim on the lapse
is extended while the handler runs and the handler is never cancelled by it, and a handler that
raises gets the lapse again once the claim has run out, `claim_for` (5 s) later. A pod that dies
mid-handling hands it to another. So the app's lapse action is idempotent and fenced by the
generation, one atomic step in the app's own store that acts only if the generation it stored is
the lapsed one; a lease that started again meanwhile has another generation and is left alone. A
failure of the lapse service, a refusal or an unexpected exception, writes `lease_lapse_service.failed`
once per streak, by type, and the service serves again after 5 s, writing
`lease_lapse_service.resumed` once it has run that long without failing; it never ends silently and
no readiness signal reflects it. The block's exit cancels the service and waits for it. Proved by
`tests/test_mcp_server_leases.py`, `tests/integration/test_redis_leases.py` and the real-Redis
cells of `tests/integration/real_redis`: a lease renewed on one pod still holds, with its
generation, on another after the first was killed, and its lapse reaches `lease_ended` once with that
generation.

**What each app owns:** its places and who stands where, and acting on a lapse fenced by its
generation, and reconciling after a Redis restart where `holds` is false.

## Hearing and speaking

A member that hears a conversation and speaks in it does both through the member module's
records, the same way in every app. The library carries no sound: the app turns speech into text
and a sentence into sound, and an agent sees only through a tool result's image
([HEARING-19](hearing.md)).

**Hearing.** Heard speech is a window entry with `spoken` true: its `text` is one finished
utterance and its `name` the speaker's shown name. Which member hears which speaker, and the
line a heard utterance becomes, is `MemberHearing`; the rules are in [hearing.md](hearing.md).

**Speaking** is the side-effecting tool `SPEAK_TOOL`, `speak`. Its arguments are
`SpeakArguments(place, text, language, style, mood, interrupted)`: the `PlaceRef`, as for `accept_invitation`,
one sentence and optionally its `language`, a lowercase ISO 639-1 code of two letters such as `de`
or `en`, its `style`, free text such as `louder and angrier` that the app passes on unchanged and
neither bounds nor checks, and its `mood`, a `FigureMood`, one of figure's moods and no free text;
a speak without one dumps without the key, and an app whose shown look plays no mood ignores the `mood`
([CALLING-38](calling.md)), and optionally `interrupted`, the id of the hold the sentence answers
instead of the held rest ([Holding and going on](#holding-and-going-on)); without it, the key stays
out of the JSON. The record checks only their form; the app decides the rest ([HEARING-20](hearing.md)), and the speech service owns the
style's bound and check ([HEARING-23](hearing.md)). On success it answers `Spoken(spoken)`,
`{"spoken": true}`, when the sentence starts to play, with no length, or `Written(written)`,
`{"written": true}`, when the app could not voice the sentence and posted it as the member's written
line. The end of a spoken sentence reaches the member as its own line in the window, whole or cut
([HEARING-16](hearing.md)). Both answers are a result without `is_error`, and `SpeakAnswer` reads
either. A refusal is an `is_error` result whose content is `SpeakRefused(refused)` as JSON, such as
`{"refused": "too_long"}`, with one `SpeakRefusal` code:

- `not_active`: the member left, was removed or is paused; a queued `speak` that an end or a pause
  drops answers it too ([HEARING-15](hearing.md));
- `too_long`: the sentence is longer than the app speaks at once;
- `unavailable`: the app neither voices nor writes the sentence now: a social reason, a limit of
  its own, or a failed voice at a place without a chat;
- `language_unavailable`: the app's voice does not speak the named language;
- `style_unavailable`: the voice refused the style: it takes no style, or the style is too long or
  not supported;
- `stopped`: a `stop_speaking`, or a `speak` naming the hold, dropped this queued sentence;
- `held`: the member is held and the sentence names no hold; it answers at once and changes nothing;
- `not_interrupted`: the sentence names an id that is not the standing hold, any id while the member
  is speaking or silent; it answers at once and changes nothing.

No refusal is ever written as a line ([HEARING-25](hearing.md)). A `speak` refused for any reason
leaves the hold and its rest as they were.

**The voice state.** The window carries `voice_state`, a `VoiceState` (`available` or
`unavailable`), when the app states one: `unavailable` means the app's own voice has failed for
that member at that place, so a runner starts a written turn; a social reason or a limit of the
app never sets it. It is absent when the app states none, and the window then dumps without the
key ([HEARING-24](hearing.md)).

**Being talked over.** While people talk over the member, its window carries `talked_over`:
`TalkedOver(id, playing, speech)` with one `PartialSpeech(name, text)` per person speaking, in the
order they started. `id` stays fixed from the record's appearance to its removal and is new for the
next one, so a runner takes one decision per `id`; `playing` says a sentence of the member plays
now, and is false while the member is held. A partial is provisional text, no `PlaceEntry`, so the `seq` rules hold beside it; a person's
partial goes with that person's turn, with or without a finished line after it, and the record goes
with its last partial. The window dumps without the key when nobody talks over the member
([HEARING-21](hearing.md)). The record arises while the member is speaking or held and a person
the member may hear starts speaking; while it is silent, none arises.

**Stopping** is the side-effecting tool `STOP_SPEAKING_TOOL`, `stop_speaking`, with
`StopSpeakingArguments(place, interrupted)`; `interrupted` is optional, and the JSON leaves it out
when unset. It cuts the member's playing sentence and drops its queue in every seat, and answers
`StoppedSpeaking(cut)` as JSON, the text of the cut sentence's line. With nothing playing and a
queue waiting, or a held sentence that never started, it drops the queue and answers
`DroppedSpeaking(dropped)`, `{"dropped": true}`; `StopSpeakingAnswer` reads either. A refusal is an
`is_error` result with `StopSpeakingRefused(refused)` as JSON: `not_active` (the member left, was
removed or is paused), `nothing_playing` (the member is silent: nothing was cut and nothing
dropped) or `not_interrupted` (`interrupted` is not the standing hold)
([HEARING-22](hearing.md)).

**Holding and going on.** When a person the member may hear starts speaking while a sentence of
the member plays or waits, the app holds the member at once, with no decision from the runner: the
playing sentence fades, and the window gains `interrupted`, `Interrupted(id)`, in the same change in
which `playing` turns false. The `id` is the app's and never repeats within the window's
`generation`. `interrupted` stands exactly while the member is held and leaves in the same change
that ends the hold. Nothing ends it by itself and no timer runs; the runner decides, and without a
decision nothing the member held or sends is said, so a `speak` without the id is refused `held`.
Three tools decide, each naming the id:

- `CONTINUE_SPEAKING_TOOL`, `continue_speaking`, with `ContinueSpeakingArguments(place,
  interrupted)`: the held sentence's rest plays, faded back in, then the queue. It answers
  `ContinuedSpeaking(continued)`, `{"continued": true}`, and refuses with
  `ContinueSpeakingRefused(refused)` as JSON: `not_active` or `not_interrupted`
  ([HEARING-31](hearing.md));
- `stop_speaking` with the id: the member is silent, the held sentence enters as a `cut` line when
  it had started, the queued `speak`s answer `stopped`, and the stop answers `StoppedSpeaking(cut)`
  or `DroppedSpeaking`;
- `speak` with the id: the answer instead. The held sentence enters as a `cut` line when it had
  started, the queued `speak`s answer `stopped`, and the new sentence takes the head of the queue,
  plays at once and answers `Spoken` or `Written`, its line after the `cut` line. The `speak`
  answer carries no cut text, so a runner reads it from the `cut` line in its window.

A person who is already speaking does not hold the member again after a continue or a `speak` with
the id; a person who starts afresh does, with a new `id`. A voice failure does not end the hold. A
new generation, the member's end, the place's end and the member's pause end it, and the new window
carries no `interrupted` ([HEARING-30](hearing.md), [HEARING-9](hearing.md)).

A runner needs no gate for sending the id: it sends only an id it read from a window's
`interrupted`, which only an app with the hold writes.

**Rollout order.** Answers and arguments are strict: a runner takes the release before its app
answers `DroppedSpeaking`, `held` or `not_interrupted`, and an app on an older release refuses a
`speak` with `interrupted`, so a runner sends it only once its app has shipped the hold.

Proved by `tests/test_member.py` (among them
`test_speak_arguments_with_a_malformed_language_are_refused`) and `tests/test_member_speaking.py`
(among them `test_a_malformed_talked_over_record_is_refused`,
`test_stopped_speaking_names_the_sentence_it_cut`,
`test_a_speak_answer_reads_spoken_and_written_each_as_its_own_record`,
`test_a_window_reads_its_voice_state_as_sent` and
`test_a_cut_mark_on_a_written_line_is_refused`,
`test_a_window_reads_its_hold_back_from_its_json_unchanged`,
`test_continue_speaking_arguments_with_a_malformed_field_are_refused` and
`test_a_stop_answer_reads_stopped_and_dropped_each_as_its_own_record`).

**What each app that hears and speaks owns**, besides what [hearing.md](hearing.md) gives it:

- It appends the member's own sentences as `own` and `spoken` when they end, a sentence cut before
  its end with `cut` ([HEARING-16](hearing.md)).
- It writes a sentence its voice fails on as the member's written line where the place has a chat,
  keeps `voice_state` true to its voice, and logs a voice going down once per outage
  ([HEARING-24 to HEARING-26](hearing.md)).
- It holds a member talked over at once, writes `interrupted` exactly while it is held, drops
  nothing and continues nothing by itself, and refuses with `held` and `not_interrupted`
  ([HEARING-30](hearing.md)).
- It plays one member's sentences in order, each behind that member's previous sentence, and
  answers each at its turn with `Spoken` when it starts to play or `Written`.
- It bounds a sentence's length, refuses with these codes, and drops what a member has not yet said
  when an end or a pause takes it, answering each unanswered `speak` `not_active`
  ([HEARING-15](hearing.md)).
- It bounds its window as it does without speech.
- Its own privacy section states what it sends and its legal basis.


## A marked route as a tool

A host turns the routes of its FastAPI app into person-front tools. `tools.agent_tool(name=…,
effect=…)` is the mark and `tools.not_an_agent_tool(reason=…)` the exclusion, each passed as a
route's `openapi_extra` and each stored under the one extension key `x-agent-tool`, so a route
holds only one. The client imports no FastAPI.

**The rule.** Every operation of the document is decided: marked, or excluded with its reason. A
route becomes a tool only through its own mark; an exclusion yields no tool and no mark.
Marks are static: `route_tools` runs once at start, and a host that offers a route's tool only at
some moments narrows the offer with `offered_tools` ([the tools of the
moment](#the-standard-path)), never the marks; the route still refuses an act its moment does not
allow.
`route_tools` decides first, over the whole document: when any operation carries neither, it
raises one `RouteProfileError` naming every undecided operation in document order, before a tool
is built or `mark_route` runs. Only then is each operation judged: an exclusion whose reason is
not a non-blank string, or an extension value that neither function returns, is refused naming
its operation. An excluded operation is never judged against the profile below: its method,
parameters, body, responses and description may be anything, which is how an upload, a stream or
a browser-only route is left out. It still counts as another operation of its method in the
overlap check. Proved by
`test_an_operation_with_neither_mark_nor_exclusion_is_refused_naming_every_undecided_one`,
`test_an_undecided_operation_is_refused_even_when_a_marked_one_breaches_the_profile`,
`test_an_exclusion_that_not_an_agent_tool_does_not_return_is_refused` and
`test_an_excluded_route_outside_the_profile_is_accepted_beside_a_marked_tool`.
`mcp_server.route_tools(openapi, app, *, mark_route)` reads the host's own `app.openapi()` and
returns `RouteTools(tools, marks)`: the `PersonTool`s, and a `frozenset` of what
`mark_route(METHOD, template)` returned for each marked operation, in the host's own mark type.
`mark_route` runs only on marked operations, never on an excluded one; its `ValueError` becomes
a `RouteProfileError` naming the operation, with the host's error as its cause, and any other
exception propagates. Proved by `tests/test_mcp_server_routes.py`:
`test_a_marked_route_becomes_a_tool_with_its_name_description_and_input_schema`,
`test_an_excluded_route_is_never_passed_to_the_host_mark_function`,
`test_the_host_mark_functions_value_error_becomes_a_profile_error_with_it_as_cause` and
`test_another_exception_from_the_host_mark_function_propagates_unchanged`.

The host wires in this order: register every route, read `app.openapi()` (FastAPI caches it),
run `route_tools`, then build the gate with the marks.

**The profile** of a marked operation. `GET`, `POST`, `PUT`, `PATCH` and `DELETE` can be marked,
`HEAD`, `OPTIONS` and `TRACE` only excluded; a `PUT`, `PATCH` or `DELETE` marked `READ_ONLY` is
refused, because RFC 9110 §9.2.1 does not count them safe and a client may run a read-only tool
without asking. Parameters are string path parameters and scalar query parameters (string,
integer, number or boolean, with or without `enum`, inline or by `$ref`, nullable through `anyOf
[x, null]`); the one body is a required `application/json` object whose fields are flattened
beside the parameters; `$ref` is inlined from `components`, and an annotation beside a reference
is kept and wins over the component's own; no `title` is left in the tool's input schema at any
depth; every success response is JSON, and an error status may
also declare RFC 9457 problem details (`application/problem+json`), beside or instead of JSON. A
success without JSON content, such as a `204`, is refused: such a route is excluded or answers
JSON. The tool's description is the operation's `description` (the route's docstring) and is
required. Everything else is refused at start with `RouteProfileError`: a mark on `HEAD`,
`OPTIONS` or `TRACE`, a mark that is not the mapping `agent_tool` returns, header and cookie
parameters, a non-JSON or optional body, a success response that is not JSON, an error response
that is neither JSON nor problem details, a recursive schema, a body object carrying any keyword
flattening would lose, a `$ref` beside any keyword that is not an annotation, a name shared by two
inputs, two marks of one name, an integer path parameter and a missing description. Proved by
`test_a_put_patch_or_delete_marked_read_only_is_refused_naming_the_operation`,
`test_a_head_options_or_trace_route_may_be_excluded_beside_a_marked_tool`,
`test_a_marked_route_outside_the_profile_is_refused_naming_the_operation`,
`test_an_error_status_may_declare_problem_details`,
`test_a_hand_written_mark_that_agent_tool_does_not_return_is_refused`,
`test_a_body_object_keyword_that_flattening_would_lose_is_refused`,
`test_a_constraint_beside_a_reference_is_refused` and `test_two_marks_of_one_name_are_refused`.

**Overlap.** Two templates of one method overlap when they have the same number of segments and
every pair of segments at a position could match one string: two literals only when equal, and
any pair holding a parameter always. A marked template that overlaps any other operation of its
method is refused whatever the order, whether the other is marked or excluded; a partial
segment may refuse a pair that never collides, never the reverse. The document cannot show
converters, routes hidden from it, a `Mount`, a WebSocket route, a catch-all or a second
registration of one method and path. A route hidden from the document, a `Mount` or a WebSocket
route never appears in it, so it can become neither a tool nor a mark. A converter is stripped
from the template (`/files/{rest:path}` reads as `/files/{rest}`), and FastAPI folds a second
registration of one method and path into one operation, so the overlap check and the profile
cannot see either: the host proves that no marked route has a converter, a catch-all or a
second registration.
Which route falls into an excluded class (deleting for good, deleting in bulk, sharing,
publishing, sending out) is judged mark by mark in each host's review, not in code. Proved by
`test_a_marked_template_that_overlaps_another_operation_of_its_method_is_refused_in_either_order`
and `test_a_marked_template_that_cannot_collide_with_another_operation_is_accepted`.

**The call.** Each tool call is one in-process ASGI request into `app`, which must be the host's
whole application, so the request passes every middleware, whatever the method
(`test_a_marked_put_patch_or_delete_is_called_with_its_path_query_and_body_arguments`). An
argument the schema does not name, a path value that is missing, not a string, empty, `.`, `..`
or holding `/`, and an object or array query value fail the call before any inner request
(`tests/test_mcp_server_route_call.py`:
`test_a_bad_path_value_or_an_unnamed_argument_is_refused_with_no_inner_request`,
`test_an_object_or_array_query_value_is_refused_with_no_inner_request`). `path` is the outer
`root_path` followed by the template with each value as given, and `raw_path` has each value
percent-encoded as UTF-8; nothing decodes either again
(`test_path_values_keep_the_decoded_path_apart_from_the_encoded_raw_path`). An absent or `null`
query value is omitted, the empty string is sent as `name=`, a string as itself and any other
scalar as its JSON text (`test_query_values_reach_the_route_as_sent`). The inner scope holds only
`type`, `asgi`, `http_version`, `scheme`, `server`, `client` and `root_path` from the outer one,
plus the call's own method, path, `raw_path`, query and headers: `Authorization`, `Host`, every
`X-Forwarded-For` line, `X-Forwarded-Proto`, and the generated `Content-Length` (and
`Content-Type: application/json` with a body). It carries no cookie, `Origin`,
`Accept-Encoding` or `state`, so the host's gate admits the inner request on its own, and a
route reading lifespan state from `scope["state"]` fails visibly
(`test_the_inner_scope_carries_exactly_the_allowlisted_keys`,
`test_the_inner_request_replays_exactly_the_named_headers`). `receive` gives the body once, then
waits until the response is complete and answers `http.disconnect`
(`test_receive_gives_the_body_once_then_blocks_until_the_response_is_complete`).

**Outcomes.** A 2xx gives the body text, never an image, and a 4xx a failed result with the route's body. A 3xx, a
5xx, a body over `MAX_EVENT_BYTES` (no byte past it is kept), a body that is not UTF-8 and an app
that returns before its response is complete give a failed result with one fixed text; only the
status and the closed `reason` are logged (`route_answer.failed`). An exception the app raises
propagates to the front, which answers its own fixed text and writes the tool call's `failed`
line. Proved by `test_a_2xx_gives_the_body_and_a_4xx_gives_a_failed_result_with_the_routes_body`,
`test_an_unusable_response_gives_one_failed_result_that_repeats_nothing_of_the_body`,
`test_a_body_exactly_at_the_bound_is_given` and `test_an_exception_the_app_raises_propagates`.

## The voice loop

An app that hears a conversation and speaks in it runs the voice loop in its own process, under
the `voice` extra:

```python
from agents.voice.loop import run_voice_loop

await run_voice_loop(
    agent, backend, person, hearing, voice, wakes=wake_rule, context=context, report=report
)
```

- `agent` is the `AgentDefinition`, with a `voice` section; without one, `run_voice_loop` raises
  `ValueError` before it reads `hearing`. The system text is its prompt, then the library's
  protocol text (`voice.reply.PROTOCOL_TEXT`: the line format, the window note, how the loop
  records replies, the reply's language and ends with `REPLY_RULES`), then `context`, the
  app's pinned part.
  `REPLY_RULES` is the one text of what a spoken reply is: heard once, plain sentences without
  markup, lists, headings or emoji, only the words said aloud and never what the agent does or
  why, `[silent]` alone to say nothing. It names no channel to answer in. A runner whose reply
  is read by `ReplyReader` outside the loop keeps it in the system text of every turn, so that
  the front stays fixed across spoken and written turns. A runner with both kinds of turn states
  in its own words which turns the rules hold for and marks each spoken turn in that turn's
  message, as `HOLD_RULES` does for a paused reply. It never adds the rules to the end of a
  spoken turn's messages. A written line's own tool description says what that line does.
- `backend` is the app's `AgentBackend`, and `person` the one `TurnPerson` the loop runs for,
  never a speaker: every decision is charged to that person's budget at the gateway.
- `hearing` is an `AsyncIterator[HearingEvent]`: a finished line (`PlaceEntry`) in increasing
  `seq`, or `PartialsChanged(speech)`, the whole partial speech at the place after a change, one
  `PartialSpeech` per person other than the agent whose finished line the app gives, in the order
  they started, empty when nobody speaks. The app gives each change whether or not the agent
  answers, and a person's finished line comes before the change that drops that utterance's
  partial. An app that yields only lines is never held. A new window generation is a new loop.
- `voice` is the app's `VoiceOut[Audio]`: `render(sentence, *, language)` returns the sound and
  neither plays nor queues it. `language` is the answered line's language: that of the newest
  line the decision sends, after the window's drops, that a person said or wrote, so not the
  agent's own, an act or the loop's pause line. It is `None` when that line names none (a written
  line, or a spoken one the app heard in no language) or the decision sends no such line, and the
  app speaks in its own default; an older line's language never stands in. A held answer that
  goes on keeps its own language, and one that replaces it takes the language of the interjection
  it answers. An app whose lines carry no language names its language in the prompt or `context`.
  The protocol text tells the model the same rule, so what it writes and what the app speaks
  agree. Proved by `tests/test_voice_language.py`. `play(audio)` returns when it has ended. A sound is heard only through its
  own `play`, and cancelling or refusing a `play` stops that sound at once. Either raises
  `VoiceRefused` when the app cannot speak now. The loop plays the sentences in order: `play` for
  a sentence starts only after it returned for the one before. While one sentence plays, the loop
  renders the next, never more than one ahead, so `render` may run while `play` does, and a
  render taking no longer than the sentence playing before it adds no pause between the two.
  `pause()` pauses the sound playing now, faded as the app's voice fades, and its `play` does not
  return while paused; it returns once the app has asked its sound to pause, so where the sound
  plays beyond a hop (a browser) the hop's time is the app's. A sound that already ended is left
  as it is. `resume()` goes on from where the sound paused, faded back in, and its `play` returns
  when it ends; when the paused sound had already ended it returns at once, and it raises
  `VoiceRefused` when the app cannot go on. The loop calls `pause` only while a `play` has not
  returned and `resume` at most once after each `pause`. A `pause` that never returns holds
  hearing.
- `wakes` is the app's `WakeRule(line, *, first_after_answer)`, asked for every line that is
  neither the agent's own, nor an echo, nor an act. `first_after_answer` is true for the first such
  line heard after a decision that played at least one sentence ended, while no decision is in
  flight, and false for every other line, one heard while an answer plays included.
- `report` receives each `VoiceEvent`: `Woke`, `StayedSilent`, `ReplyUnreadable`,
  `SentenceStarted(text)`, `SentenceRefused(text)`,
  `DecisionFailed(kind, code, detail, retry_after_s=...)`, whose `outcome` is the stop's
  `StopOutcome`, `DecisionEnded`, `EchoDropped(text)`, `Held` and `HoldEnded(ending)` with the
  `HoldEnding` `continued`, `finished`, `stopped`, `answered` or `dropped`.

**Being talked over.** The loop holds its answer itself, from the partial speech it is given, and
reads neither `talked_over` nor `interrupted`, which stay the member wire's hold for a runner
outside the app ([hearing and speaking](#hearing-and-speaking)); HEARING-32 in `docs/hearing.md`
is the rule. From `Woke` until the answer has ended, a hold included, the first partial with a
letter or digit of a person's utterance that began during the answer reports `Held`, pauses the
sentence that plays and starts no other, also while the reply is still written. A partial
without words never holds, and neither does an utterance that began before the answer, however
long it goes on. A person who starts while the talk-over stands joins it; a start after it ended
is a newer talk-over. The agent's own voice is the app's to keep out, by echo cancellation: the
loop does not test partials for echoes, so an echo the app lets through holds the answer once per
sentence that plays.

The talk-over ends once every utterance in it has ended (a person's finished line, or a change
without that person). Then, once the held turn has been read to its end, tool rounds included, so
that no two decisions overlap, one more decision opens over every line heard since, a finished
line of the interjection among them, and a pause line, one JSON object with `paused_at` (the
sentence that was playing, or `null`) and `unsaid` (the sentences written and not started,
joined). The wait for the held turn is bounded by its `maxDurationSeconds`; a person whose
question the speech service splits into two finals wakes the agent on the first and holds it on
the second before anything has played, so the first model call is spent for nothing and the
decision waits for the whole first reply. Both costs are among those the app's own latency measurement reads. Only the first opening read in the decision's reply decides:

| Reply opens with | `HoldEnded` | What the loop does |
| --- | --- | --- |
| `[continue]` | `continued` | resumes the paused sentence; the held queue plays on |
| `[finish]` | `finished` | resumes the paused sentence; when it has ended, drops the queue and the sentences rendered ahead |
| `[silent]` | `stopped` | stops the paused sentence and drops the queue |
| speech | `answered` | stops the paused sentence, drops the queue and plays this decision's sentences |
| anything else, no opening, or a failed decision | `dropped` | as `stopped`; its lines stay undecided |

After a marker nothing later in that turn is spoken, and its tools still run to their end, also
once the sound has stopped; the next decision starts after them, unless the app refused the
sound. The decision's
reply is recorded as the sentences that started after the ending, or, when none did, the marker
it wrote; a drop records nothing and leaves the interjection's lines to the next decision. Only a
reader made for a hold reads `[continue]` and `[finish]`; in any other decision they are
unreadable. No timer ends a hold: a talk-over that never ends keeps it, and when hearing ends
while held the loop closes the turns, stops the sound and returns.

The events of a hold follow this order, and `Held` and `HoldEnded` alternate strictly:

```text
Woke  [StayedSilent|ReplyUnreadable]*  SentenceStarted*      the answer
  Held                                                        a talk-over holds it
  Woke  [StayedSilent|ReplyUnreadable|DecisionFailed]?        the hold decision
  HoldEnded(continued)  SentenceStarted*                      the queue
| HoldEnded(finished)                                         the resumed sentence plays out
| HoldEnded(answered)   SentenceStarted*                      the new sentences
| HoldEnded(stopped | dropped)
  [SentenceRefused]                                           a refused resume or play
  (Held ... HoldEnded)*                                       a newer talk-over
  DecisionEnded                                               the span is over
```

A talk-over that starts while the decision is in flight reports nothing until the ending is
known. If the ending plays something (`continued`, `finished`, `answered`) and that talk-over
has a word, `Held` follows at once and the ending's effects wait: the paused sentence is not
resumed and no new sentence starts. What the ending would play is the held answer, and the
newer talk-over gets its own decision over it once the first decision's turn was read to its
end. If the ending plays nothing, the answer has ended and the newer talk-over's lines wake as
any line. A talk-over that starts after the ending, while its sentences play, holds them as any
answer. Proved by `tests/test_voice_hold.py`:
`test_the_first_words_of_a_talk_over_pause_the_playing_sentence_and_start_no_other`,
`test_a_talk_over_without_words_never_pauses_the_answer`,
`test_an_utterance_that_began_before_the_decision_never_pauses_its_answer`,
`test_a_second_person_who_starts_during_the_answer_holds_it_while_an_earlier_speaker_goes_on`,
`test_a_talk_over_while_the_reply_is_still_written_starts_no_sentence_until_the_ending`,
`test_the_hold_decision_starts_once_the_last_speaker_finished_and_sends_what_was_said_and_unsaid`,
`test_the_hold_decision_waits_for_the_held_reply_to_be_written_to_its_end`,
`test_continue_resumes_the_paused_sentence_and_plays_the_rest_of_the_answer`,
`test_finish_resumes_the_paused_sentence_and_drops_the_rest`,
`test_silent_stops_the_paused_sentence_and_says_nothing_more`,
`test_speech_in_the_hold_decision_replaces_the_rest_with_no_further_request`,
`test_a_hold_reports_woke_held_woke_and_its_ending_in_this_order`,
`test_an_ending_waits_while_a_newer_talk_over_stands_and_that_talk_over_gets_its_own_decision`,
`test_a_second_hold_decision_waits_while_the_first_one_is_still_being_written`,
`test_a_talk_over_that_starts_while_an_ending_resumes_the_sound_holds_it_again`,
`test_the_tools_of_a_hold_decision_run_to_their_end_after_its_marker_ended_the_hold`,
`test_the_next_decision_sees_what_was_said_before_and_after_the_hold`,
`test_an_utterance_that_gains_words_only_after_the_answer_ended_holds_nothing`
and `test_a_newer_talk_over_after_a_stopping_ending_holds_nothing_and_its_line_wakes_as_any_line`.

Proved by `tests/test_voice_loop.py`:
`test_a_spoken_line_that_wakes_sends_one_request_with_prompt_protocol_context_and_the_line`,
`test_sentences_play_in_order_and_the_first_starts_while_the_reply_is_still_written`,
`test_the_first_line_after_an_answer_that_played_is_the_one_marked_first_after_answer` and
`test_no_line_after_a_decision_that_played_nothing_is_marked_first_after_answer`; the missing
`voice` section by `test_an_agent_without_a_voice_section_is_refused_before_hearing_is_read` in
`tests/test_voice_window.py`; the render ahead by `tests/test_voice_render_ahead.py`:
`test_the_next_sentence_renders_once_written_while_the_one_before_it_plays`,
`test_a_sentence_two_ahead_renders_only_after_the_one_playing_has_played`,
`test_sentences_play_in_written_order_each_started_after_the_one_before_it_played` and
`test_a_render_taking_no_longer_than_the_sentence_playing_before_it_adds_no_pause`.

**The standard wake rule.** `agents.voice.wake.addressed_or_asked(names)` returns
a `WakeRule`, a cheap local check of which lines cost a decision. It wakes on a line that names
one of `names` as a whole word in any case, on a line that ends with `?` before its trailing
spaces and closing quotes, and on the first line after an answer that played. Another agent's
line wakes by the same rule, and a line that does not wake still enters the conversation.
`names` without a name or with an empty one raises `ValueError`. Proved by
`tests/test_voice_wake.py`:
`test_a_line_naming_one_of_the_names_as_a_whole_word_in_any_case_wakes`,
`test_a_line_ending_in_a_question_mark_wakes_and_one_asking_only_mid_line_does_not`,
`test_a_rule_without_a_name_or_with_an_empty_one_is_refused` and
`test_in_the_loop_a_named_asked_or_first_line_after_a_played_answer_wakes_and_all_are_heard`.

**The echo rule.** The agent's own voice, heard back through a person's microphone, arrives as
that person's spoken line; the loop drops it so the agent does not answer itself. A spoken line
that is not the agent's own is an echo when it has counted words and at least half of them are
counted words of the agent's latest answer. A counted word is split at whitespace, stripped of
the punctuation at its edges and still longer than two characters, and is compared in lower
case; a short reply such as "Ja, ok." therefore always enters, and a word that is only
punctuation counts on neither side. The latest answer is the most recent decision that
played at least one sentence; its words are those of every sentence that started playing, the
one playing now included, never a queued one, one rendered ahead or a refused one. An echo does not
enter the conversation, is not asked of `wakes`, leaves `first_after_answer` to the next line and is
reported as `EchoDropped(text)`. It keeps its place in `seq` order like the agent's own spoken line:
an echo out of order ends the loop like any line, and it never counts in the next line's
`missed_before`. A written line is never an echo. The rule is a text-level guard, so it misses an
echo mixed with more words of a person, and it drops what a person said when at least half of its
counted words are the latest answer's; `EchoDropped` lets an app count the drops.
Proved by `tests/test_voice_echo.py`:
`test_a_spoken_line_is_an_echo_when_half_its_long_words_are_words_of_the_answer`,
`test_a_word_counts_and_compares_without_the_punctuation_at_its_edges`,
`test_only_a_spoken_line_that_is_not_the_agents_own_can_be_an_echo`,
`test_a_spoken_line_repeating_the_answer_is_dropped_never_asked_and_never_sent`,
`test_an_echo_heard_while_the_next_decision_is_read_repeats_the_answer_before_it`,
`test_a_line_repeating_only_an_older_answer_enters_while_the_latest_one_plays`,
`test_a_line_repeating_only_an_older_answer_enters_while_the_next_decision_is_read`,
`test_a_written_line_with_the_answers_words_enters_and_is_asked`,
`test_a_short_spoken_reply_to_the_agents_question_wakes_it_as_the_first_line_after`,
`test_a_sentence_still_queued_does_not_count_while_the_one_before_it_plays`,
`test_a_sentence_still_rendering_does_not_count_and_the_one_played_before_it_does`,
`test_a_refused_sentence_does_not_count_and_the_one_played_before_it_does`,
`test_after_a_decision_that_played_nothing_the_answer_before_it_is_still_the_latest`,
`test_an_echo_that_breaks_its_order_ends_the_loop_closed_and_silent` and
`test_the_line_after_an_echo_counts_only_the_lines_never_heard_as_missed`, and by
`test_a_line_repeating_only_the_sentence_rendered_ahead_is_no_echo_and_enters` in
`tests/test_voice_render_ahead.py`.

**The conversation.** Every line enters one running conversation, but an echo and the agent's
own spoken line: the loop records what it played itself. An act line enters, is never asked of
`wakes` and leaves `first_after_answer` to the next line, so a person's short reply after an
answer and an act is still the first line after it. Within the window below, a decision
sends the pairs decided so far and one user message with every line since the last decision, one
JSON object per line with `name`, `kind`, `own`, `spoken` and `text`, `act` only on an act line,
and `missed_before`, the
number of `seq` values skipped, after a gap. The assistant message is what played, or
`[silent]`; a hold decision's is what played after its ending, or its marker. The conversation lives in memory while the loop runs and is stored nowhere. Proved by
`test_a_line_that_does_not_wake_sends_nothing_and_leads_the_next_decisions_message`,
`test_an_own_spoken_line_never_enters_and_an_own_written_line_enters_but_never_wakes`,
`test_a_gap_in_seq_marks_the_next_line_with_how_many_were_missed` and
`test_a_silent_reply_speaks_nothing_runs_its_tools_and_is_recorded_as_silent`.

**The window.** Each decision sends only a window of the conversation, so a call does not cost
more the longer a talk runs. Between drops the messages only grow, so a request's prefix changes
only at a drop. Dropping decided pairs frees a quarter of the window, so a talk whose decisions
are recorded drops once per quarter it grows, never once per line. Undecided lines that alone
exceed the window lose only what they must, down to the window, because freeing more would drop
more lines the agent never saw; so while decisions give their lines back (a failed turn, an
unreadable reply, a refusal before anything played), each of them drops again and rewrites the
prompt. The bound limits the bytes sent, never whether the agent answers, and what a decision
dropped stays dropped, even when its lines stay undecided.

| Bound | Value | Over it |
| --- | --- | --- |
| One decision's messages | the agent file's `windowBytes`, in UTF-8 bytes of the messages' texts, because the library cannot count a provider's tokens; the system text does not count and never goes | the oldest whole decided pairs go until a quarter of the window is free; then, while the undecided lines alone exceed the window, the oldest of them go down to it; the newest line always stays, even when it alone exceeds the window. From the first drop on, `voice.conversation.WINDOW_NOTE` opens the first user message |

Every line that is no echo enters the conversation, and a decided pair leaves the window only
after it was sent.
The oldest undecided lines a drop takes are the one exception: they may never reach any request, and
only `WINDOW_NOTE` tells the agent that earlier parts are no longer shown.

Proved by `tests/test_voice_window.py`:
`test_a_talk_within_the_window_drops_nothing_and_each_request_extends_the_last`,
`test_a_request_beyond_the_window_drops_the_oldest_whole_pairs_until_a_quarter_is_free`,
`test_a_long_talk_drops_once_per_quarter_of_the_window_it_grows_never_once_per_line`,
`test_lines_beyond_the_window_on_their_own_lose_their_oldest_and_keep_the_newest`,
`test_a_line_larger_than_the_window_still_goes_whole_and_the_agent_answers` and
`test_pairs_a_decision_dropped_stay_dropped_and_the_note_stays_when_it_played_nothing`; the
note's sentence in the protocol text by
`test_the_protocol_text_names_the_note_that_opens_a_window_with_earlier_parts_dropped` in
`tests/test_voice_reply.py`.

**The order.** At most one decision is in flight and one answer plays. A line that wakes
meanwhile is held, and once both are done exactly one decision follows. Without a talk-over the
answer plays uncut; with one, the next decision waits until the chosen ending has played, and
the hold decision opens over every undecided line and clears a pending wake. The loop has no cap and
no rate limit; the person's budget at the gateway bounds it. Proved by
`test_two_lines_heard_while_deciding_of_which_one_wakes_get_exactly_one_more_decision`,
`test_lines_heard_while_deciding_that_do_not_wake_start_nothing_and_lead_the_next`,
`test_a_waking_line_heard_while_an_answer_plays_is_decided_after_it_plays_uncut` and
`test_two_hundred_waking_lines_each_after_the_last_answer_get_two_hundred_decisions`.

`DecisionEnded` is the last event of a decision a waking line started, its holds included: it
comes after every other event of that decision (`Woke`, `StayedSilent`, `ReplyUnreadable`,
`SentenceStarted`, `SentenceRefused`, `DecisionFailed`, `Held`, `HoldEnded`). By then its sound has
ended, each sentence it started having played to its end, been refused or been stopped, and every
turn it started, a hold decision's and their tools included, has ended: read to its end, stopped
with `TurnStopped`, or closed after a refused sound. The next `Woke` comes after it. The first
`Woke` after the loop starts or after a `DecisionEnded` opens a span; a `Woke` inside an open span is
a hold decision and gets no end of its own. `EchoDropped` is hearing's, not a decision's, and may
come before or after it. When the loop itself ends (hearing ends, an error, the app's cancel), no
`DecisionEnded` is reported; the app frees what it holds when `run_voice_loop` returns, raises or
is cancelled. Proved by `tests/test_voice_decision_end.py`:
`test_a_decision_reports_its_end_once_its_last_sentence_has_played`,
`test_a_silent_unreadable_or_failed_decision_reports_its_end_once`,
`test_a_refused_sound_reports_the_end_after_the_turn_was_closed`,
`test_a_decision_reports_its_end_only_after_its_tools_have_run`,
`test_an_answer_with_a_hold_reports_two_woke_and_one_end_after_its_ending_has_played` and
`test_a_stopping_or_dropping_hold_reports_the_end_after_the_paused_sentence_was_stopped`. The loop's
own end reports no `DecisionEnded`, proved by `test_a_voice_that_raises_another_error_closes_the_turn_and_raises_it`
in `tests/test_voice_loop.py` and by `test_when_hearing_ends_while_held_the_loop_stops_the_paused_sound_and_returns`
in `tests/test_voice_hold.py`.

**The reply** is read per round, and a tool call ends a round. Leading whitespace is skipped. A
round that opens with `[` is held until `]` closes it within 16 characters; the word between,
trimmed of spaces and folded to ASCII lower case, is the marker. `silent` speaks nothing of the
round and its tools still run; any other word, no `]` in reach or a round that ends first speaks
nothing of it and reports `ReplyUnreadable`. Any other round is speech, a `[` inside it included:
a sentence is released once content follows it, a number and its period read as a German
ordinal, and the rest when the round ends. A sentence of a speech round that is nothing but
`[silent]` (in the case and spacing the opening accepts) is dropped, the sentences before it
stay spoken, and any other bracket stays text. The reader of a hold decision also reads
`continue` and `finish` as markers that mute the round; they are the loop's, never speech.
Proved by `tests/test_voice_reply.py`:
`test_a_rounds_opening_marker_decides_whether_any_of_it_is_spoken`,
`test_a_tool_call_ends_a_round_and_the_next_round_is_read_afresh`,
`test_a_sentence_is_released_only_once_content_follows_it` and
`test_an_answer_cut_anywhere_releases_each_sentence_once_whole_and_after_content`, by
`test_a_tool_rounds_text_plays_and_its_rest_is_released_when_the_round_ends` in
`tests/test_voice_loop.py` and by `test_text_splits_into_sentences_and_an_ordinal_does_not_end_one`
in `tests/test_voice_sentences.py`.

**Failures.**

- `VoiceRefused` ends the answer: the turn is closed, the queued sentences are dropped, a
  rendered one included, and `SentenceRefused` is reported; a render refused while the sentence
  before it plays is reported once that one has played. Proved by
  `test_a_refused_render_ends_the_answer_closes_the_turn_and_keeps_what_played`,
  `test_a_render_refused_while_the_one_before_it_plays_is_reported_after_that_one_played` and
  `test_a_refused_play_never_plays_starts_or_records_the_sentence_rendered_after_it`.
- `TurnStopped` is reported as `DecisionFailed` with the stop's `retry_after_s`; the sentences
  already queued play out and the held rest is dropped. Proved by
  `test_a_turn_stopped_after_a_sentence_played_plays_out_the_queued_one_and_drops_the_rest`,
  `test_a_turn_stopped_while_a_sentence_plays_still_plays_the_one_rendered_after_it` and
  `test_a_busy_refusal_reports_its_wait_on_the_failed_decision`.
- A decision that played nothing and was not silent leaves its lines undecided, and only a new
  waking line starts the next decision, so a spent budget never makes the loop retry on its own.
  Proved by `test_a_decision_that_played_nothing_leaves_its_lines_undecided_until_a_line_wakes`.
- The turn is read to its end in its own task while the answer plays, so `maxDurationSeconds`
  bounds the writing, never the playing. Proved by
  `test_playing_longer_than_the_turns_limit_plays_every_sentence_without_a_limit_failure`.
- When hearing ends, the loop closes the turn, stops the sound and returns. Any other error of a
  port or the backend, and a `seq` not above the last, closes the turn, stops the sound and is
  raised inside an `ExceptionGroup`. On the app's cancel `CancelledError` propagates, the sound
  stops, the render ahead is cancelled and nothing more is reported; none of these ends a
  decision with `DecisionEnded`. Proved by
  `test_when_hearing_ends_the_loop_closes_the_turn_stops_the_sound_and_returns`,
  `test_a_backend_error_other_than_a_stopped_turn_is_raised`,
  `test_a_hearing_that_breaks_its_order_or_raises_ends_the_loop_closed_and_silent`,
  `test_a_voice_that_raises_another_error_closes_the_turn_and_raises_it`,
  `test_the_hosts_cancel_closes_the_turn_stops_the_sound_and_reports_nothing_more` and
  `test_the_hosts_cancel_stops_the_sentence_playing_and_the_render_after_it`.
- A hold decision that fails, opens unreadable or opens nothing ends the hold as `dropped`: the
  paused sound stops, the rest is dropped and nothing more is said. A `resume` the app refuses
  reports `SentenceRefused` for the paused sentence and ends the answer, as a refused play does.
  The decision waits for the held turn to be read to its end, which `maxDurationSeconds` bounds.
  Proved by
  `test_a_failed_unreadable_or_empty_hold_decision_drops_the_rest_and_says_nothing`,
  `test_a_resume_refused_ends_the_answer_as_a_refused_play_does` and
  `test_when_hearing_ends_while_held_the_loop_stops_the_paused_sound_and_returns`.
- A port that never returns holds the answer while hearing goes on; the app bounds its own ports.
  Proved by
  `test_a_voice_that_never_returns_still_hears_lines_but_starts_no_decision_until_cancelled`.
