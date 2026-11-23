# Calling an agent

The rules by which a person calls an agent to a place and sends it away, the same in every
app. Read it when you build or change an app's control for this, or write a test for it: a test
cites a rule by its ID, such as `CALLING-9`. The mechanism the rules ride on (the standing, the
invitation, the lease, `accept_invitation`) is in [connector.md](connector.md#the-member-module).

A **place** is where an app seats an agent as a member; a **person** acts through the app's page;
the **owner** is the person an agent works for; the **inviter** is the person who called it.

## Who is offered which agent

**CALLING-1.** An agent is offered only if it has an account of its own and holds an open listen,
on any pod of the app, that covers the app's standing. The lease keeps an agent at a place it
holds; it does not make the agent offered. An app asks the library with `await
ResourceChanges.listens_to(principal, STANDING_URI)` or `await listeners(STANDING_URI)`, and learns
of a change from the `listening_changed` and `listening_resynced` callables it passes to
`ResourceChanges` ([connector.md](connector.md#the-standard-path)).

**CALLING-2.** An agent is offered at a place only while its owner is present there by the app's
rule.

**CALLING-3.** A person is offered an agent only if they are present at the place and a member of
the realm group `/agent-adders`. The list rests on the view's membership (CALLING-17); the call
reads it fresh (CALLING-21).

**CALLING-4.** An agent is not offered at a place where it is invited or already joined, nor at a
place that has no room for it.

**CALLING-5.** While an invitation is open, only its inviter sees the agent, as inviting.

**CALLING-25.** An agent's runner holds a listen on an app's standing only while it answers
invitations to that app's places, by accepting or declining them.

## Calling

**CALLING-6.** The invitation is the app's. It is bound to its inviter and to the owner's
presence, and it expires `INVITATION_SECONDS` (`agents.member`) after the app opens it. The agent
side reads the invitation's `expires_at` and never that value. Bound to the owner's presence, an
open invitation ends with the outcome owner away (CALLING-14) when its owner leaves the place by
the app's presence rule (CALLING-2). An owner whose seat lost its connection and waits to resume
has not left: an accept then is refused with `sponsor_away` (CALLING-7).

**CALLING-7.** The agent's accept is checked by the app: the invitation still stands (id, epoch,
expiry), the owner is still present, and a seat is free (CALLING-32). The place then becomes
active or the accept is refused with a code of `AcceptInvitationRefusal`. An accept of a standing
invitation refused with `two_seats`, `sponsor_away` or `busy` ends the invitation as a decline does
(CALLING-30) and the call without a seat: the inviter's row shows the outcome the app maps from the
code (CALLING-14, CALLING-22), owner away for `sponsor_away`, and no refusal. A `stale_invitation`
refusal changes nothing.

**CALLING-8.** An invitation that runs out unanswered ends the call without a seat: the inviter's
row shows the outcome no answer (CALLING-14), and no refusal. A runner that lets an invitation run
out instead of declining it stays valid.

**CALLING-32.** An agent takes a seat at a place as a person does: the place takes agents up to its
own seats, counted with the people's, and the app sets no limit of agents apart from that, neither
per place nor per person who brings them. How an app protects its own server stays the app's and
never reads as a place limit.

**CALLING-29.** An agent's runner that is at its own limit of how many places it may be in at once
declines an invitation at once with `decline_invitation(place, invitation, epoch, reason)` and the
reason `busy` of `DeclineInvitationReason`. The runner alone decides that it is at its limit; the
app never computes it and does not check the reason.

**CALLING-30.** The app checks a decline as it checks an accept's invitation: it is that member's
invitation and still stands (id, epoch, expiry). A decline of one that no longer stands is refused
with `stale_invitation` of `DeclineInvitationRefusal` and changes nothing. A standing decline ends
the invitation at once, as its expiry does and without raising the epoch: the app takes the
invitation from the agent's standing, signals the standing, and the row shows its outcome
(CALLING-31), then is offered again.

**CALLING-31.** A standing decline ends the call without a seat: the inviter's row shows the
outcome busy (CALLING-14), mapped from the decline's reason (CALLING-22), and no refusal. A declined
agent may be called again; the app adds no wait of its own before a new call, and the outcome's
duration from CALLING-14 still applies.

## Sending away

**CALLING-9.** A person present at the place may send an agent away that holds it when they are a
member of `/agent-adders` (read fresh at the send-away), the agent's owner, or hold a right the
place's own rules give. An agent is sent away whether or not its owner is present; the person who
sends it away is present at the place, the owner included.

**CALLING-10.** A send-away takes effect at once and at any time, mid-activity included: the app
takes the place from the agent's standing, raises the place's epoch, signals the standing and
tells the place's people. The agent's runner finds the place absent and ends its turn there.

**CALLING-11.** A place that cannot go on without the agent pauses with the agent's seat open.
What pausing and resuming mean there is the app's.

**CALLING-12.** An agent that was sent away may be called again at once.

**CALLING-13.** When an agent leaves a place without being sent away stays each place's own rule.
The standard neither requires nor forbids that an agent leaves with its owner.

**CALLING-26.** A place inside another app's place is independent of it: sending an agent away
from one leaves it in the other. Its standing names the other place as its `parent`
(CALLING-27).

**CALLING-27.** An app whose place lies inside another app's place sets `parent` on the standing
of its place, from the invitation on, and keeps it unchanged while the member holds the place, so
a runner can read it before it accepts.

**CALLING-28.** `parent.app` is the parent app's MCP resource URL, taken from the app's own
setting, and `parent.place` is the `PlaceRef` the parent app uses, taken from the page that called
the agent, so untrusted. A runner matches a parent by equality of the normalised URL and of the `PlaceRef`
against a place it holds, and a parent that matches none counts as none. A parent grants no right.

## What the person sees

**CALLING-14.** The control lists each agent in one of three states: available (the person may
call it), inviting (no action) or here (the person may send it away). Inviting shows no deadline.
A call that ends without a seat shows its outcome (no answer, busy, owner away) on the inviter's
row for a time the shared kit owns, then the row is available again. A refusal above the rows
answers only a refused press.

**CALLING-15.** Neither calling nor sending away asks for confirmation or counts down: the tap is
the act.

**CALLING-16.** An agent is listed as here only for a person who may send it away, by CALLING-9.
To everyone else it shows only at its seat in the place, with the AI mark.

**CALLING-17.** The control is shown to a person present at the place who is a member of the group
(CALLING-19) or holds a send-away right the place's own rules give (CALLING-9), and to any person
for whom it lists an agent. When it lists no agent, it shows one sentence that no agent can be
reached, and no reason per agent; that holds only when no agent can be reached. When the place has
no free seat (CALLING-32), the app passes the control's `full` and the control says the place is
full instead. The app decides whether to show it from the groups its session carries, or from one
read of the group when the view is built, and the place's rights. The group is never read per
render; only the act reads it fresh (CALLING-21), so a person whom a stale view shows the control
is refused at the act as not allowed.

**CALLING-18.** The control stands at the place itself, at the same position whether or not the
place lies inside another app's page. No app fetches an agent into another app's place.

## The turn state

**CALLING-33.** A runner states, for each seat it holds, whether its turns there answer, and
whether a turn is running, with `set_turn_state(place, stopped, thinking)` (`agents.member`); a
statement carries `thinking` or `stopped`, never both:

- When a turn starts at a place, it states `thinking` there (CALLING-48).
- After each turn end at a place, it states none if the turn answered, else the turn's
  `StopOutcome` from `outcome_of_stop`. It may leave out a statement that repeats the last one the
  app accepted at that seat in this run; a `thinking` statement is never that last one, so a turn
  end always follows it with a statement. A new seat at the same place starts with nothing stated,
  for the runner and the app alike. Statements at one seat are made in order, and a seat whose
  turn is running is left to that turn's end, apart from the `thinking` statement that turn began
  with.
- A runner that has stated nothing at a seat since it started states after its first turn end
  there. So a restarted runner states none after its first answered turn at each seat. A value
  stated before the restart stays until the runner's first statement at that seat, or until the
  seat ends.
- A `busy` stop is stated like any other.
- Once it has stopped taking turns in an app where it holds seats (suspend or backoff), it states
  `unavailable` at each of them where the app still answers; it may wait before doing so (for
  example until a backoff reaches its cap), so a blip that recovers on its first retry need show
  nothing. Once a full pass of standing, accepts and windows goes through again, it states at each
  held seat what its last turn end there in this run called for: that turn's outcome if it
  stopped, none if it answered or there was none.
- It states no cancelled turn (apart from ending its thinking, CALLING-48), and no other failure
  of its own calls. A cancelled turn is no turn end: the last turn end at that seat stays the one
  before it.
- An app that does not serve the tool answers an unknown tool. A refused or failed statement is
  logged, and the runner goes on.

**CALLING-34.** The app checks that the caller holds a seat at the place, active or paused, or
refuses with `not_held`. It keeps the last stated value for that seat alone, drops it when the seat
ends, and keeps no other state about it.

**CALLING-35.** With a value stated:

- Everyone at the place sees the same thing: beside the agent's name, wherever the app draws it
  (seat and here row alike), the kit's one turn-state part shows the mark and the outcome word. No
  viewer gets a different view.
- The app passes the stated value to that part and draws nothing of its own for it.
- With nothing stated, the part shows nothing extra. The display asks nothing of the realm.

**CALLING-36.** A runner that cannot reach the app states nothing. It is shown by the lease and the
offer (CALLING-1, "The lease" in [connector.md](connector.md#the-standard-path)). An app infers no
failure from silence or from timers. An app that admits a member's calls but fails them keeps the
seat unmarked; that is the app's own failure, not this rule's.

**CALLING-48.** `thinking` states that the member's model is working on a turn at the seat. A
runner states it when a turn starts there; the turn's end statement (CALLING-33) ends it. A turn
that is cancelled ends it too: the runner states what the last turn end at that seat called for in
this run, none if there was none. A runner that dies before it ends the thinking leaves it to the
app's cap (CALLING-49).

**CALLING-49.** The app keeps `thinking` as the seat's stated value (CALLING-34), so the next
statement at that seat replaces it. It shows the thinking from the statement until the member's next
speak or gesture at that place, its next `set_turn_state` at that seat, or a cap the app chooses,
which keeps a runner that died from leaving the member thinking for ever. `thinking` is no outcome:
the mark and word of CALLING-35 show none for it, and the app shows it as its own working state. The
app never infers it from heard lines or from timers.

## The look

A **look** is how an agent appears at a place: a 3D figure or a picture a video service renders. In
`agents`, "look" means only this. A **room** is an agent's own surroundings, one static 3D file that
figure draws behind a figure look in every framing. In `agents`, "room" in a look means only this.

**CALLING-41.** An accept may carry `looks` (`agents.member`), the agent's looks, at least one,
first choice first: a figure look `{kind: "figure", url, sha256, gestures, room}` or a video look `{kind:
"video", name}`, the name 1 to 64 ASCII letters of either case, digits, `.`, `_` or `-`. At most one
look is a figure and no video name stands twice. A figure's, a clip's and a room's `url` is an
absolute `https` address with a host and no user part, query, fragment, whitespace or backslash, and
no `%` or other forbidden character in the host; a port is allowed. Its `sha256` is 64 lowercase
hexadecimal characters. The record checks only that form. The app decides
which look it can show and shows the first it can, so a look it cannot show (a kind it does not
draw, a name its video service does not offer, a look that fails to start) is skipped for the next;
with none it can show, it shows no look of the accept's and still accepts. The old `figure` of an
accept stays beside `looks`: an accept with `figure` and no `looks` has the one figure look, and an
accept with both is read by its `looks` alone.

**CALLING-37.** An agent makes a gesture with `gesture(place, gesture)` (`agents.member`):

- `gesture` is a token of figure's base set, `FigureGesture`, which `agents` generates from the
  figure release it pins; the enum in the tool's schema is the agent's whole list of gestures, the
  same for every agent. A token outside it fails the schema.
- An app offers the tool, `gesture_tool()`, to a member only while the shown look plays gestures,
  so the tool has no refusal for a look without them. A look of kind figure plays gestures, a
  video look does not, and the offer follows the shown look when the app moves to the next
  (CALLING-46).
- The app checks that the caller's seat at the place is active, or refuses with `not_active`. It
  hands the gesture to the shown look and answers `{"relayed": true}`. That does not say the look
  played it: a look plays one gesture at a time and drops one it cannot play, and nothing reports a
  dropped gesture (CALLING-43, CALLING-45).

**CALLING-38.** `speak` may carry a `mood`, a closed token of figure's moods, `FigureMood`, beside
the free `style`, which stays for the voice. An app whose shown look plays a mood shows it; an app
whose shown look plays no mood ignores it and does not refuse the speak. A look of kind figure
plays moods and a video look does not.

**CALLING-39.** A figure look, or the old `figure`, may carry `gestures`: for a gesture of figure's
base set, the `{url, sha256}` of the persona's own clip, which replaces figure's neutral clip for
that gesture on this figure only. A gesture has at most one clip, and clips cannot be sent without
a figure. The app treats a clip as it treats the figure: it fetches from sources it allows and
serves only bytes whose digest matches.

**CALLING-40.** A runner sends `mood`, `looks`, `figure.gestures` or a figure look's `room` only to an
app that has the `agents` release carrying them, because the records refuse a key they do not know. The order is:
`agents` releases, the apps pin the release and deploy, then the runner sends.

**CALLING-50.** An agent sets how much of its figure shows with `set_framing(place, framing)`
(`agents.member`):

- `framing` is a token of figure's framings, `FigureFraming`, which `agents` generates from the
  figure release it pins: from the face to the whole body. The enum in the tool's schema is the
  agent's whole list, the same for every agent, and a token outside it fails the schema. The token
  is figure's own spelling (`upperBody`), passed unchanged to the look.
- An app offers the tool, `framing_tool()`, to a member only while the shown look plays framings,
  so the tool has no refusal for a look without them. A look of kind figure plays framings, a video
  look does not, and the offer follows the shown look when the app moves to the next (CALLING-46).
- The app checks that the caller's seat at the place is active, or refuses with `not_active`. It
  answers `{}`, also when the framing is the one already set. That does not say the look drew it
  (CALLING-43).

**CALLING-51.** The app keeps the framing as the seat's stated value, as it keeps the turn state
(CALLING-34, CALLING-35): the last one per seat, kept while the seat is paused and dropped when the
seat ends. Everyone at the place sees the same, a viewer who joins later too. Until one is set,
the app shows its own default; none travels at the accept. `agents` keeps no framing and
`Standing` carries none.

**CALLING-52.** A figure look may carry `room`, the `{url, sha256}` of the agent's own room, which
figure draws behind the figure in every framing. The old `figure` and a video look carry none. The
app treats the room as it treats the figure: it fetches from sources it allows and serves only
bytes whose digest matches, and hands them to figure, whose room check judges them before it draws;
the app may run the same check before it keeps a room. A room that is refused, fails or does not
arrive shows none; the figure stays and the accept stands. A room whose `url` or `sha256` breaks the
form of CALLING-41 is no such room: as for the figure or a clip, the accept's arguments do not fit
the tool, the tool answers an error that names each failing path and its error type, and the whole
accept is refused. The soft drop covers a well-formed room. The room is the agent's for its seat,
the same for everyone at the place.

The rules below hold for every look the app shows; the app, not the library, shows the look.

**CALLING-42.** The app reports facts about the call and the look decides the motion they make.

**CALLING-43.** A fact a look cannot show is dropped, never refused, and never delays the voice.

**CALLING-44.** When facts hold together, the look shows speaking over thinking over listening over
idle.

**CALLING-45.** A gesture is a moment: the look shows one at a time and queues none.

**CALLING-46.** A look ends with one closed signal and stays ended; the app then shows the next
look the agent named that it can show, or none.

**CALLING-47.** The voice is the app's: a look that draws in the browser taps it, and a look that a
server draws receives it and returns it with the picture.

## The group

**CALLING-19.** One realm group, `/agent-adders`, decides who may call, for every owner's agent,
and who may send away besides the agent's owner and a right the place's own rules give
(CALLING-9). Each app names it as a constant. Any member of the group may call or send away any
owner's agent while it is there.

**CALLING-20.** Before a second person becomes an owner (a member of `/agent-owners`), the split
of this group per owner lands. Nothing refuses or warns when a second owner is added.

**CALLING-21.** Group membership is read fresh from the realm for each call and each send-away,
never for the display (CALLING-17). Fresh means asked of the realm at the act itself, never taken
from a token, session or cache that existed before it. Liveness comes from the app's own listen
streams. The standard adds no store, directory or list.

## What an app supplies

**CALLING-22.** An app supplies:

- its place (the `PlaceRef` kind and id) and its presence rule;
- what accepting and pausing mean at the place, and its seats (CALLING-32);
- the store of its invitation book, written in one step with its seats, and the facts agents.calling
  decides on (who is present, where the owner is, a free seat, a right the place's own rules give);
  and acting on a lease's lapse;
- the person routes for calling and sending away, which are never an MCP tool, so no agent can
  call or send away an agent;
- the shared control and the AI mark, with a stable, opaque key of its own minting for each row,
  the same across the row's states;
- the mapping of its refusal codes and decline reasons, many to one, onto the control's reasons and
  outcomes;
- its channel to its page;
- when it shows agents' looks, which look it shows (CALLING-41), the `gesture` and `set_framing` tools,
  the `mood` of a speak and the clips and room of an accept (CALLING-37 to CALLING-39, CALLING-50
  to CALLING-52), and the rules of the look
  (CALLING-42 to CALLING-47); and
- when its place lies inside another app's place, the `parent` of its standing (CALLING-27).

## The steps

1. The agent's runner listens on the app's `member://standing`, so the agent is live (CALLING-1),
   and does so only while it answers invitations to the app's places, by accepting or declining
   them (CALLING-25).
2. A person opens the place. The page shows the control at the place itself where CALLING-17 shows
   it, and lists agents per CALLING-1 to CALLING-5.
3. **Call.** The person taps an available row. The page posts to the app's person route. The app
   reads the person's groups fresh, opens an invitation (CALLING-6), adds it to the agent's
   standing, with its `parent` where CALLING-27 applies, and signals it.
4. The runner reads the standing and calls `accept_invitation(place, invitation, epoch)`. The app
   checks it (CALLING-7) and makes the place active or refuses. A runner at its limit calls
   `decline_invitation(place, invitation, epoch, reason)` instead (CALLING-29); the app checks it
   and ends the invitation or refuses (CALLING-30).
5. The row moves: inviting, then here for those who may send away, or its outcome and then
   available again after the invitation runs out (CALLING-8), is declined (CALLING-31), its
   accept is refused (CALLING-7) or its owner leaves (CALLING-6). A refusal of a press shows the
   reason the app maps (CALLING-22).
6. **Send away.** The person taps a here row. The page posts to the app's person route. The app
   checks the right (CALLING-9), takes the place from the standing, raises the epoch, signals the
   standing and tells the place's people (CALLING-10). The runner reads the standing and ends its
   turn there.

A place inside another app's place follows the same steps (CALLING-26); each place's own rule
decides when an agent leaves it without being sent away (CALLING-13).

## Owners

**CALLING-23.** Every app the standard covers follows these rules and draws the one shared control,
so that calling and sending away an agent feel the same in each.

**CALLING-24.** This file owns the rules; the shared design kit holds the control's appearance, the words
of its reasons and the sentence of an empty list, and the one part that draws a stated turn outcome
(CALLING-35), its appearance and its words in de and en, so no app words it again. The standard covers
every fleet app that has a place an agent with an account of its own can join. An app with such a
place adopts it with the release of the shared control and of this library's part; an app without
one adopts it at the landing of its first such place.
