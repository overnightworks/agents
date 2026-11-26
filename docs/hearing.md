# Hearing an agent

The rules by which an app lets an agent hear, and see, the people at a place, the same in every
app. Read it when you build or change an app's way to let an agent hear or see, or write a test for
it: a test
cites a rule by its ID, such as `HEARING-4`. The records the rules ride on (the place window, the
`spoken` entry) are in [connector.md](connector.md#hearing-and-speaking).

A **place** is where an app seats an agent as a member; a **speaker** is a person whose sound or
picture the app captures at a place; a **member** is an agent that may hear or see. The library
carries no sound: the app turns speech into text, and a finished utterance reaches the library as a
record. A picture reaches an agent only as a tool result (HEARING-19).

## The mental model

`MemberHearing` (`agents.mcp_server`) is a table in memory: for each place, speaker and sense, the
members switched on and the moment each was switched on. The app's rule decides who may hear whom
and tells the table with `allow`. When a speaker's utterance is finished, the app asks `heard`,
which answers the members that hear it and the one line to append. A member hears an utterance
only if it was switched on no later than the utterance began; any end deletes the entries, so
nothing that finishes after it reaches anyone.

Each rule is marked **L** when this library's tests prove it, naming the test, and **A** when each
app proves it.

## Who may hear

- HEARING-1: (A) Only the app's own rule decides who may hear whom, and the app records every
  decision with `allow(place, speaker, member, sense, allowed)`. A person's speech is entered only
  while that person allows it under the app's own rule, so the app records `allowed` true for a
  member of a person speaker only while that person allows it, and false when the person withdraws
  it.

- HEARING-2: (L) The record is keyed by place, the app's opaque speaker key, member and `Sense`;
  `Sense.HEARING` and `Sense.SEEING` (HEARING-19) are defined, each with its own entries, and `heard`
  reads hearing alone. The speaker key is the app's own, not a `Principal`, so a seat
  can end while the account goes on; the same key at two places names two speakers. Proved by
  `test_one_speaker_key_at_two_places_is_two_speakers` and
  `test_a_switch_on_for_one_speaker_is_not_one_for_another_speaker` and
  `test_a_seeing_switch_is_its_own_and_lets_no_member_hear`.

- HEARING-3: (L) Nobody is heard before an `allow` is recorded. Proved by
  `test_nobody_hears_before_an_allow_is_recorded`.

## What is heard

- HEARING-4: (L) The rule of the moment: a member hears an utterance only if its entry exists
  when `heard` runs and its switch-on time is no later than `Utterance.began_at`. `allow` with
  `allowed` true while on keeps the earlier time; `allowed` false deletes the entry. A switch-on
  in the middle of an utterance therefore loses that whole utterance, and so does a switch-off and
  on during it: the cost of never delivering words said before the member was switched on. Proved by
  `test_a_member_hears_an_utterance_only_if_the_switch_on_is_no_later_than_its_start`,
  `test_a_switch_off_before_heard_runs_loses_the_utterance`,
  `test_a_switch_off_and_on_again_hides_only_what_began_before_the_second_switch_on`,
  `test_a_second_allow_while_on_keeps_the_earlier_switch_on_time` and
  `test_each_member_hears_by_their_own_switch_on_time`.

- HEARING-5: (A) `began_at` is a lower bound of the sound's start on the clock the record
  reads: the default is the running loop's time, read when `allow` runs, or the callable passed to
  `MemberHearing(now=...)`, and both the record and the app use that one clock. The app maps it
  from the speech service's position of the utterance's start in the audio against the moment its
  server opened the sound session (which is no later than the session's first frame), never from
  the moment an event arrives. When it has no position, it uses the previous utterance's mapped
  end, or that session start.

- HEARING-6: (L+A) Only finished utterances are heard: one spoken line per utterance, under the
  speaker's shown name and the app's `kind`, and never provisional text as an entry; provisional text
  reaches a member only through HEARING-21, and the voice loop only through HEARING-32. `heard` builds the line as
  `SpokenLine` (`spoken` true, `own` false) for every hearing member, carrying as `traceparent` the
  trace `heard` runs in, so the app calls it inside the request that heard the utterance and a runner
  continues that trace in the turn the line wakes; the app appends only finished utterances. Proved
  by `test_heard_builds_one_spoken_line_of_the_utterance_for_the_hearing_members`,
  `test_the_heard_line_carries_the_trace_the_utterance_was_heard_in` and
  `test_the_line_is_spoken_and_not_the_members_own_and_becomes_a_place_entry_once_seq_is_added`.

## Where hearing ends

- HEARING-7: (L+A) A member's end (`member_ended`), a speaker's end (`speaker_ended`) and a
  place's end (`place_ended`) each end hearing at once, for an utterance that finishes after it
  included. After a rejoin, only an utterance that began after the new `allow` is heard. The app
  reports every exit path: leaving, removal, an ended lease, the speaker's leaving and the place's
  ending. Proved by `test_an_end_ends_hearing_at_once_for_an_utterance_that_finishes_after_it`,
  `test_after_a_rejoin_only_an_utterance_that_began_after_the_new_allow_is_heard` and
  `test_an_end_reaches_only_its_member_speaker_or_place`.

- HEARING-8: (A) The record does not know who has joined. The app calls `allow` only for a
  member joined at that moment and never after it has reported that member's, that speaker's or
  that place's end; a default recorded at a join is not replayed after a leave.

- HEARING-9: (A) Pausing an agent does not end its hearing; an app that wants it to records
  that with `allow`. A speaker's parked seat is not an end: the app ends that speaker's sound
  session and keeps the record, and reports `speaker_ended` when the speaker leaves. A pause is named
  beside an end, never called one: it cuts the playing sentence as an end does (HEARING-16), drops
  the queue and the held rest (HEARING-15) and ends the hold, so `interrupted` leaves the window
  (HEARING-30); a `talked_over` record that stands keeps following HEARING-21, as it does while the
  member is silent.

- HEARING-10: (L) `allows` and `sensing` answer from the same entries as `heard`, so the app's
  gate and its ear mark agree with what is delivered: `sensing` names every member switched on, one
  switched on in the middle of a running utterance included, who does not yet hear it. Proved by
  `test_allows_and_sensing_answer_as_heard_does_for_an_utterance_after_every_switch_on`,
  `test_allows_and_sensing_answer_nobody_after_an_end` and
  `test_sensing_names_a_member_whose_switch_on_is_later_than_a_running_utterance`.

## Where the line goes

- HEARING-11: (A) The library returns lines and owns no window. The app gives each line its
  `seq`, appends it to the window of each member in `Heard.members`, and signals the change with
  `ResourceChanges.changed` once the append is visible to a read.

## The record's life

- HEARING-12: (A) The record lives in one process. The app calls it on the front's event loop
  or through `anyio.from_thread.run_sync`; a call from another thread is not safe.

- HEARING-13: (L) The record is in memory only: nothing is persisted, an utterance's text and
  name pass through `heard` and are kept nowhere, and no call logs. Proved by
  `test_no_call_writes_a_log_record` and
  `test_the_record_keeps_neither_an_utterance_nor_its_line_once_the_caller_lets_go`.

## An agent's sentences

- HEARING-14: (A) An agent's sentences at a place take their turns in the order of their `speak`
  calls. A sentence's turn comes when the sentence before it has ended; the first one's comes at
  once. A voiced sentence ends where the app measures the end of its sound, or when it is cut
  (HEARING-16); a written one ends as it enters. A sentence that names the hold's id
  (`SpeakArguments.interrupted`, HEARING-30) takes the head of the queue: it plays at once, after the
  hold's rest is dropped, and its line enters after the `cut` line of the held sentence, when one entered.

- HEARING-15: (A) The app's `speak` answers at the sentence's turn, not before: `Spoken` when the
  sentence starts to play, with no length, or `Written` when it enters as a written line (HEARING-25).
  The end of a voiced sentence reaches the member as its own line (HEARING-16), never as a length.
  A sentence a hold stopped reaches the member first as the hold's `place` (HEARING-30). A sentence
  that not every seat heard a word of yields no line (HEARING-16); the member learns it from the
  hold's `place` (`heard_to` 0) or from the stop's `DroppedSpeaking` (HEARING-22). Held, it yields its
  line only once it plays again to its end.
  An argument refusal (`too_long`, `language_unavailable`, `style_unavailable`), a standing
  refusal (`not_active`) and the refusals of the hold (`held`, `not_interrupted`; HEARING-30) answer
  at once. `stopped` answers when the stop, or a `speak` naming the hold, drops the sentence. When
  the member leaves or is removed, when its lease or the place ends, or when the member is paused,
  the app drops what the member has not yet said at once, and every `speak` not yet answered, a held
  sentence that never started included, answers `not_active`: no `speak` waits past it. The other
  `unavailable` refusals, a social reason or a limit of the app, answer when the app knows, at the
  latest at the sentence's turn; a failed voice at a place without a chat answers `unavailable` at
  the sentence's turn, where `Written` would otherwise have answered. The shape of the answers is
  proved by `test_a_speak_answer_reads_back_from_its_json_as_the_marker_alone`,
  `test_a_speak_answer_whose_marker_is_not_exactly_true_is_refused` and
  `test_a_speak_answer_with_an_unknown_or_missing_field_is_refused`.

- HEARING-16: (A) An agent's sentence enters the windows at the place once it has been said, as a
  person's line does (HEARING-6): when it ends, as a spoken line in every window at the place, `own`
  in the member's own. A sentence cut before its end (by a stop, by the member's end at the place, by
  its pause, by a hold, or by a voice failure after it started to play) enters with `cut` and only the
  text every seat heard up to the cut, to a word's end; for a hold that is `sentence[:heard_to]` of
  its place. While the app cannot tell how far it played, the line carries the whole sentence; never a
  guess from the time it played. A line is never changed once appended. Every sentence answered
  `Spoken` yields at most one own line in the speaker's window, whole or `cut`, also after a voice
  failure that follows the start. It yields none when not every seat heard a word of it: cut, it
  enters no window; held, it stays the head of the held queue, and continued, it plays from its start
  and enters whole at its end. A sentence dropped by a stop or by a `speak` naming the hold answers
  `stopped` and yields none. A sentence dropped by an end or a pause answers `not_active`. A sentence
  that never started, refused or dropped from the queue, enters no window, a held sentence dropped
  before it started included, and no person's line is ever cut. The app marks an agent's spoken line
  `continues` when the same speaker's next sentence at the place already waits to play as the line
  enters, and copies a fact it holds rather than deciding anything; it never marks a person's line or
  a cut line, and a sentence still being rendered leaves the line unmarked, the safe direction. The
  library decides nothing on the mark: whether a marked line wakes the member stays the runner's wake
  rule.

- HEARING-17: (A) The app's server keeps the queue of an agent's sentences, the written ones
  included. The library orders no sentences.

- HEARING-20: (A) An agent names the language of its sentence; the app never guesses it from the
  text. Without one the app speaks the sentence in its page's language. A language the app's voice
  does not speak is refused at once with `language_unavailable`, as `too_long` is.

## Being talked over

- HEARING-21: (A) When a person's voice at a place has lasted the app's bound (the speech service's
  event for a lasting voice, about 0.4 s), and `allows` admits the agent to hear that person at that
  moment, and a sentence of the agent plays or waits then, or the agent is Stopping or Held
  (HEARING-30), the app sets the window's `talked_over` record: its `id`, `playing` and a
  `PartialSpeech(name, text)` for that person, whose `text` may still be empty. A voice that ends before
  the bound creates no record and reaches the member only as its line. `playing` is true only while a
  sentence of the member plays at that moment, and stays true through Stopping; it is false while one
  only waits or the member is Held, and turns false in the change that adds `interrupted`. The `id` is
  the app's, never repeats within the window's `generation` and stays fixed from the record's
  appearance to its removal; a second person who starts speaking meanwhile adds a partial under the
  same `id`, and `speech` keeps the order in which they started. A runner takes at most one decision
  per `id`. The app updates the partials as the words come and `playing` as the agent's sentences end
  and start: `playing` stays false while the agent is Held, returns to true when a new sentence of the
  member starts while the record stands, and the `id` stays the same. A sentence that plays is not in
  the window yet: when it ends or is cut without a hold, its line enters in the same window change in
  which `playing` turns false, or `playing` stays true when the member's next sentence starts at once.
  When `playing` turns false because the member is held, the line of a sentence the hold stopped before
  its end does not enter yet; it enters per HEARING-30. A sentence that played to its end while
  Stopping enters its line in that change. It signals each update per HEARING-11. A person's finished
  line replaces that person's partial in the same window change, so one read never shows both. A
  person's partial goes when that person's turn ends, also a turn that heard no words, when a
  switch-off stops the agent hearing that person (and does not return for that utterance) and when
  that speaker ends; the other speakers' partials stay. The record goes with its last partial, and at
  once when the member or the place ends; the next record has a new `id`. While the agent is silent,
  no record arises. When the record arises while the agent is Speaking, the agent is Stopping at once
  and Held once the sound has stopped (HEARING-30). After a `continue_speaking` or a `speak` that
  names the hold's id, a person who is already speaking does not hold the agent again; a person whose
  new voice lasts the bound does, with a new `id`. A partial can therefore go with no line after it,
  and a runner treats a partial as provisional. A person's turn that began before the agent has a
  sentence that plays or waits, or is Stopping or Held, creates no record; the app judges this by the
  turn's speech start.

- HEARING-22: (A) `stop_speaking(place, interrupted)` cuts the agent's playing sentence in every
  seat and drops its whole queue there, and answers `StoppedSpeaking(cut)` with the text of the cut
  sentence's line when a cut line entered. `interrupted` is optional; one that is not the standing hold's id is refused
  `not_interrupted` and changes nothing. With nothing playing and a queue waiting, a held sentence that never started or that not
  every seat heard a word of, or a playing sentence cut before every seat heard a word of it, the stop
  drops the queue and answers `DroppedSpeaking(dropped: true)`: no cut line entered. Each
  dropped queued `speak` answers `stopped`, so no call hangs. `StopSpeakingAnswer` is
  `StoppedSpeaking | DroppedSpeaking`. When the agent is silent it answers `nothing_playing`, and
  when the member left, was removed or is paused it answers `not_active`; either way nothing is cut
  or dropped. A stop is not a mute: the agent speaks again at its
  next `speak`. The cut sentence's line enters per HEARING-16 no later than `stop_speaking` answers,
  and `cut` is that line's text; the dropped sentences enter no window.

- HEARING-23: (A) `speak` may carry a `style`, free text that the app passes on unchanged to its
  speech service. The app neither bounds nor checks it: the speech service owns its length bound and
  its check. Every refusal of the style by the voice (a voice that takes no style, a style too long,
  or one the voice does not support) reaches the agent as `style_unavailable`, with no second code.

## Holding and going on

- HEARING-30: (A) A member at a place is in exactly one state: Speaking (a sentence plays, or one
  waits), Stopping (HEARING-21 held the member while it was Speaking; the playing sentence runs to its
  stop), Held(id, place) (nothing plays; the held sentence and the queue wait, either possibly empty)
  or Silent (nothing plays and nothing waits).

  - The hold arises when HEARING-21 holds the member while it is Speaking: the member is Stopping at
    once, and from then no sentence of it starts. The playing sentence plays to its end when less than
    the app's bound (about 1 s) of it remains, the remainder read from the speech service's word marks
    or, for a sentence without word marks, from its finished stream; otherwise it stops at its next
    word end, read from those marks, or at once for a sentence without word marks. Until its marks or
    its finished stream have arrived, the remainder is unknown, and the sentence stops as when more
    than the bound remains.
  - Stopping ends once the sound has stopped in every seat, for any reason, a voice failure included,
    or once the app's bound for the seats' reports has passed. When nothing plays as the hold arises,
    it ends in the same change. The record may go meanwhile; the hold still arises.
  - While Stopping, `interrupted` is absent and the tools answer as in Speaking. A `speak` without id
    queues and is held with the queue. `stop_speaking` without id cuts the sentence at once, drops the
    queue and leaves the member Silent, so no hold arises. Any id is refused `not_interrupted`. The
    member's, the place's or the generation's end and the member's pause act as in Speaking.
  - When Stopping ends, the member is Held, also when the sentence played to its end and nothing
    waits. The window gains `interrupted: Interrupted(id, place)` and `playing` turns false in one
    change. In that change a sentence that played to its end enters its line, and one that a voice
    failure ended while Stopping enters as at a voice failure while held. The `id` is the app's and
    never repeats within the window's `generation`.
  - `place` says where the hold stopped the member, and stays as it was while the hold stands:
    - `in_sentence`, with the `sentence` that answered `Spoken` and that the hold stopped before its
      end, and `heard_to`: the code points every seat heard, to a word's end, read from the word marks
      against the smallest position a seat reports. It is 0 when not every seat heard a word, and
      absent when the app cannot tell (no word marks, or a seat that did not report within the bound).
      It is never estimated from time.
    - `between_sentences`, when no sentence that answered `Spoken` was playing, or the playing one
      played to its end. A sentence still rendering counts as waiting.
    - An app on a release before the place leaves it absent.
  - `interrupted` stands exactly while the member is Held. It leaves the window in the same change
    that ends the hold: a `continue_speaking`, a `stop_speaking`, a `speak` with the hold's id, the
    member's or the place's end, or the member's pause (HEARING-9). A new generation carries none.
    Nothing leaves Held by itself, and no timer runs.
  - Without the agent's decision, nothing it held or sends is said: a `speak` without the id is
    refused `held` at once. A `speak` refused for any reason (its own arguments, `not_active`) leaves
    the hold and its rest as they were.
  - The held rest is dropped for good only at a stop, at a `speak` with the id, at the member's pause
    (HEARING-9), or at the member's, the place's or the generation's end. A held sentence with
    `heard_to` above 0 enters with `cut` and `sentence[:heard_to]`, or whole when the place states no
    `heard_to`. One with `heard_to` 0, or one that had not started, enters no window.
  - A voice failure while held does not end the hold: the held sentence enters as above, and the hold
    and `interrupted` stay over the queue. A later `continue_speaking` plays the queue, as today.
  - A new window generation ends a hold as a stop without id does.
  - `stop_speaking` with the standing id ends the hold with Silent and answers `StoppedSpeaking(cut)`
    when the held sentence's cut line entered, else `DroppedSpeaking`; without an id it does the same
    in Held. A `speak` with the standing id enters the held sentence's cut line under the same
    condition, answers the queued `speak`s `stopped`, ends the hold and plays at once (HEARING-14);
    the runner reads the cut text from that line, since the `speak` answer carries none. A `speak`,
    `stop_speaking` or `continue_speaking` with any other id is refused `not_interrupted`, in every
    state; a stale id changes nothing.

- HEARING-31: (A) `continue_speaking(place, interrupted)` ends the hold with the standing id: the
  held sentence's rest plays on from where it stopped, faded back in, then the queue, and
  `interrupted` leaves in the same window change; a sentence with `heard_to` 0 plays from its start. It answers `ContinuedSpeaking(continued: true)`, and refuses `not_active`, or
  `not_interrupted` when no hold or another one stands. The fade and the resume belong to the app's speech service.

- HEARING-32: (L+A) An app that runs the voice loop gives its hearing, besides each finished
  line, every change of the partial speech at the place as `PartialsChanged`: one `PartialSpeech`
  per person other than the agent whose finished line it would give (HEARING-1, HEARING-4), in the
  order they started, empty when nobody speaks, whether or not the agent answers. The app gives a
  person's partial only once their voice has lasted its bound (HEARING-21), so a shorter voice reaches
  the loop only as its line. A person's finished line comes before the change that drops that
  utterance's partial. The loop holds its answer itself and reads neither `talked_over` nor
  `interrupted`, which stay the member wire's hold for a runner outside the app: while it answers (the
  reply written or spoken), the first partial of an utterance that began during the answer stops the
  playing sentence (`VoiceOut.pause`) and starts no other. An utterance that began before the answer
  never stops it. `pause` stops the sound as HEARING-30 stops a playing sentence and returns the code
  points every listener heard. The loop runs it in a task of its own, so hearing goes on, and opens
  the hold decision only once it has returned. The decision is told the place (`told_hold`): the part
  heard and the rest, nothing heard, or the whole sentence when the app cannot tell, and the sentences
  written and not started. A person who starts while the talk-over stands joins it; a start after it
  ended is a newer talk-over. The talk-over ends when each of its utterances has ended. Then, once the
  sound has stopped and the held turn has been read to its end (its `maxDurationSeconds` bounds that
  wait), one decision gets the talk so far, the lines heard since and the place with what was unsaid,
  and its reply's opening ends the hold: `[continue]` resumes the stopped sound and the rest plays,
  `[finish]` resumes it and drops the rest, `[silent]` stops, speech replaces the rest; anything else,
  a failed decision or none drops the rest and nothing more is said. An ending that plays something
  waits while a newer talk-over stands, and that talk-over gets its own decision. No timer ends a
  hold. The agent's own voice is kept out by the app's audio path; the loop does not test partials for
  echoes. A person whose question the speech service splits into two finals wakes the agent on the
  first and holds it on the second, so the first model call is spent for nothing and the hold
  decision waits for that reply to be written. On the member wire `continue_speaking` cannot express
  "finish": it plays the queue too. Proved by `tests/test_voice_hold.py` and
  `tests/test_voice_hold_place.py`; the app proves its partials and its `pause` and `resume`.

## When the voice fails

- HEARING-24: (A) The app sets the window's `voice_state` for the member at that place:
  `unavailable` while the app's own voice has failed there, so that a `speak` would not be voiced for
  that reason, and `available` while it works; it signals each change per HEARING-11, and a runner
  then starts a written turn instead of a spoken one. A social reason (speaking switched off, a
  missing consent) or a limit of the app never changes it: there `speak` still refuses `unavailable`,
  and a runner never writes on its behalf. The app sets `available` again as soon as the voice works;
  how it learns that is its own. An absent `voice_state` states nothing, and a runner then behaves
  as without the field.

- HEARING-25: (A) When the app cannot voice a sentence for a failure of its voice before the
  sentence starts to play and the place has a chat, it posts the sentence there at its turn as the
  member's written line, which enters every window with `spoken` false, and `speak` answers
  `Written`. Without a chat it refuses with `unavailable`. No other refusal is ever written: not
  `not_active`, `stopped`, `held` or `not_interrupted`, not a refusal of the sentence's own arguments (`too_long`,
  `language_unavailable`, `style_unavailable`), and not a social reason, such as speaking switched off
  at the place or a missing consent, which answers `unavailable`. A failure after `Spoken` was
  answered is a cut sentence (HEARING-16), never a written line.

- HEARING-26: (A) The app logs one line when the member's voice at a place turns `unavailable`
  for a failure, once per outage for that member at that place, the first fallback included: a closed
  reason code and the place, never the sentence, a token or the voice's name. It logs nothing more
  while the voice stays `unavailable`; the next turn to `unavailable` after `available` logs again.

## What an app supplies

- HEARING-18: (A) The library owns no sound, no camera, no speech client, no token, no switch and no mark. An
  app supplies:

  - its rule for who may hear whom, and every `allow` and end that follows from it;
  - the finished utterance with its `began_at` (HEARING-5);
  - the sound, the speech client, and the verified identity of the speaker;
  - the switch and the ear mark, from the shared design kit, with the mark showing `sensing`;
  - the window, its `seq`, its append and the signal (HEARING-11);
  - the queue of an agent's sentences, the voice state, the written fallback and its log line
    (HEARING-14 to HEARING-17 and HEARING-24 to HEARING-26);
  - the talked-over record with the partials, the stop and the style (HEARING-21 to
    HEARING-23), the hold, the continue, the `speak` with the id and the refusals `held` and
    `not_interrupted` (HEARING-30 and HEARING-31);
  - for the voice loop, the partial at a lasting voice, and the stop and resume of its sound with where
    it stopped (HEARING-32);
  - what seeing needs of it (HEARING-19); and
  - its privacy text, which states what is heard and seen and its legal basis.

## Owners

- HEARING-19: (L+A) Every app the standard covers follows these rules, so that who may hear or
  see whom works the same in each. This file owns the rules; the shared design kit holds the
  switches and the marks. The standard covers every fleet app that lets an agent with an account of
  its own hear or see people at a place; an app that does adopts it with the release of this
  library's part. Seeing is the second sense: an agent sees a person only as one still picture it
  asks for through a tool of the app, which returns the picture as the `image` of its `ToolOutcome`
  for the agent's own model to read in that turn
  ([connector.md](connector.md#the-tool-contract)). The app records the person's see switch with
  `allow` and `Sense.SEEING`, apart from hearing: one switch never moves the other, and every end of
  HEARING-7 ends both. Nobody is seen before a seeing `allow` is recorded, and the app records none
  when a member joins, so the switch starts off for every member, the person's own agent included,
  each time it joins a place; a request to see that the switch does not allow answers a refusal and
  no picture. The app owns the rest: its seeing tool, the capture and what it captures, its bound on
  pictures, the mark at each picture and how long it holds a picture. The library keeps a picture only
  in the turn that reads it and writes it to no log record. Proved by
  `test_a_seeing_switch_is_its_own_and_lets_no_member_hear`,
  `test_nobody_is_seen_before_a_seeing_allow_and_again_after_an_end_and_a_rejoin` and
  `test_an_end_ends_seeing_as_it_ends_hearing`.
