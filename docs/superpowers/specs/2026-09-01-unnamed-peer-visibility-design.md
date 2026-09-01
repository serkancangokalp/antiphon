# Unnamed Codex Peer Visibility Design

**Date:** 2026-09-01

**Scope:** Make unnamed interactive Codex sessions observable enough to explain
and refuse proven ambiguity. This wave does not invent an alias, make a session
id routable, change explicit `ANTIPHON_NAME`, or implement automatic identity.

## 1. Problem and dependency boundary

An unnamed Claude channel registers under the reserved `<unnamed>` key. An
unnamed Codex session registers nothing: both `register_codex_peer` and
`record_codex_session` return before writing. Consequently two unnamed Codex
terminals can be live while `resolve_target` sees zero peers and its legacy
fallback chooses the newest running rollout. A terminal before its first user
turn has no rollout at all, so even that incomplete census misses it.

The smallest safe repair is visibility before addressability:

- record the canonical session identity Codex itself supplies to the
  `SessionStart` hook, without treating it as a peer name;
- show only what Antiphon can prove it observed, with lower-bound wording when
  an unobserved pre-first-turn session cannot be excluded;
- refuse a bare send once two or more candidate sessions are proven;
- preserve the existing single-unnamed-session fallback and every explicitly
  named route;
- tell the person to restart the intended terminal with `ANTIPHON_NAME` rather
  than presenting a session id as an address.

This is dependency group B1. B2 may later make identities automatic and
addressable after both hosts have equivalent, measured identity joins. B3
(same-vendor bridging) follows those foundations because same-kind routing is
not useful while its default peers cannot be distinguished safely. This order
is dependency ordering, not a priority demotion.

## 2. Measurement contract, fixed before the probe

The interactive pre-first-turn window is not inferred from `codex exec` or a
rollout created after a prompt. One controlled run must measure the real
installed interactive CLI before product behavior or tests are finalized.

### 2.1 Isolation and evidence

The probe uses a fresh temporary Git project and a project-local Codex hook
whose only job is to capture the host's `SessionStart` JSON. It does not install
Antiphon into the live project, does not run setup against the user's project,
and does not read or write a live Antiphon cursor, catalog or peer registry.
The temporary hook records only field names plus the `cwd`, event name and
canonical session id needed by this measurement; it records no prompt or
transcript content.

Before launch, take read-only snapshots of:

- interactive Codex processes whose cwd can be attributed to the temporary
  project;
- filenames and lock state under Codex's existing `thread-writer-locks`
  directory;
- rollout inventory and `session_meta` records attributable to the temporary
  project;
- the bytes or absence of Codex's global configuration/state files that a
  trust decision could mutate.

Repeat the first three snapshots at four boundaries: CLI ready but before any
user turn, immediately after one inert text-only turn, after clean exit, and
after the process is gone. Record elapsed boundaries, CLI version and only the
minimum session-id prefix needed to join observations in the report. The host
may naturally leave its own rollout history; it is named as probe residue and
is not deleted. If reaching the ready state requires accepting a trust choice
or any global config/state mutation, abort rather than authorizing it. Any
unexpected global byte change fails the probe and is reported; it is not
reverted silently.

### 2.2 Questions and branch consequences

The probe answers these questions in order:

1. Does an interactive Codex invoke the project `SessionStart` hook before its
   first user turn, with a canonical `session_id` and the exact project cwd?
2. Does it already hold `thread-writer-locks/<session_id>.lock` at that point,
   and does the lock remain held through the turn and become acquirable or
   disappear after exit?
3. Does any rollout file exist for that session before its first turn? If so,
   is its `session_meta.cwd` already available?
4. Independent of the hook, can a stable, version-detectable process observable
   distinguish "interactive Codex launched here, no turn yet" from "no Codex
   session here" without reading environment secrets or guessing through a
   wrapper process?

The implementation branch is predetermined:

- **A — hook id plus held lock before the turn.** Persist one non-routable
  observation keyed by the full canonical id. A reader treats it as live only
  while that exact id's writer lock is held. This is the preferred B1 design.
- **B — hook id before the turn, no reliable lock.** Persist the observation,
  but do not claim current liveness from age or file presence. B1 may report
  that an unnamed session was observed and must use lower-bound/unknown-current
  wording; routing changes are limited to ambiguity proved by another live
  source.
- **C — no hook id, but a stable project-scoped process observable exists.**
  Report only an anonymous lower-bound count. Do not synthesize ids, associate
  rollouts by recency, or make the observation routable.
- **D — nothing project-scoped exists before the first turn.** Record this as
  the finding. B1 must not claim exhaustive counts; it may improve post-hook
  visibility only, and every status/refusal sentence says additional pre-turn
  sessions may be invisible.
- **Unstable or version-specific observable.** Feature-detect it and treat its
  absence as unknown, never as zero. Do not productize a wrapper/process guess
  merely because it worked once.

The measurement result and the selected branch are appended to this design in
a separate commit and returned to Claude for a second approval before product
code begins.

### 2.3 Probe result: unmeasured at the trust boundary

The approved probe was attempted with Codex CLI 0.151.0 as
`codex -s read-only -a never --no-alt-screen` in the fresh temporary Git
project. In at most 1.2 seconds the TUI reached its directory-trust screen,
before the project hook or an interactive session started. The operator chose
`No, quit`; no trust was accepted.

The safety checks held:

- the bounded `SessionStart` capture was never written;
- the thread-writer-lock count remained four and the probe added no lock;
- a structured scan of every rollout `session_meta` found zero records whose
  cwd was the probe project;
- the launched pid exited;
- SHA-256 remained byte-identical for `config.toml`,
  `.codex-global-state.json`, `session_index.jsonl` and `history.jsonl`.

This is **not branch D**. The probe did not reach the pre-turn boundary, so it
measured neither the hook timing nor the process/file/lock observables. The
result is `U — unmeasured, blocked by trust`, and no claim from A/B/C/D follows
from it.

A possible retry at a deleted path that still has a persisted trust entry was
explicitly rejected. New bytes at an old trusted path inherit a security
decision made about different content; using that fact to avoid the prompt is
the shape of a stale-trust attack even when this hook is benign. The stale
entries are recorded in BACKLOG for the user rather than used by the probe.

The selected B1 branch is therefore deliberately conservative:

- assume the pre-first-turn blind window may exist;
- write a non-routable observation whenever a hook actually supplies a
  canonical session id, without claiming when the first such hook occurs;
- use the already feature-detected exact thread-writer lock only to prove that
  an observed id is live; a missing/unavailable lock is unknown, not dead;
- always render the census as a lower bound, including zero;
- refuse only ambiguity proved by two or more live observations or by a live
  observation alongside a registered candidate.

Branch A is excluded because pre-turn hook and lock timing were not observed;
B is excluded because even hook-before-turn was not observed; C and D are
excluded because the process-observable questions were never reached. This is
evidence-based exclusion, not preference. A later probe performed with the
user present may select A and tighten wording, but it cannot retroactively make
this run evidence of completeness.

## 3. Proposed B1 data and privacy contract

Under selected branch U, an unnamed Codex hook writes a versioned, hook-owned
observation beneath the project's `.antiphon` state whenever the host supplies
a canonical session id, keyed by that full id. It contains only kind, session
id, observed time and the project identity implicit in its directory. It never
stores prompt text, transcript content or a transcript path. Atomic per-session
files avoid a shared read-modify-write document. A malformed id writes nothing.
The observation is currently live only when the existing feature-detected
exact thread-writer lock proves it; absence of that proof is unknown.

Observation records are not peer records:

- their ids do not pass the alias grammar;
- `resolve_target(..., alias=<uuid>)` still refuses the value as an unusable
  peer name;
- they never acquire a socket address or an alias directory;
- an explicit `ANTIPHON_NAME` endpoint/session join remains authoritative and
  suppresses the same host session's unnamed presentation when that join is
  provable.

Local `status` and `doctor` may display the full canonical host session id only
for an unnamed observation, labelled `unnamed` and `not addressable`. This is a
deliberate revision of the current blanket "never show a session id" status
test: the host-provided id is now the only collision-free fact that lets a
person distinguish two observed terminals. Addresses, transcript paths and
content remain forbidden. Delivery refusals use counts, not ids, because an
agent cannot route to them; they include restart-to-name guidance.

No screen says simply "N sessions are live" in branch U. It says `at least N
Codex sessions observed` and, where relevant, that additional sessions before
their first hook/turn may be invisible. Zero observations is never rendered as
proof that zero sessions exist. A later branch-A measurement would require a
new contract change before any wording became exact.

## 4. Routing contract

B1 changes only proven ambiguity:

- two or more live unnamed observations, or named plus live unnamed candidates,
  make a bare Codex send refuse before legacy rollout selection;
- the refusal reports an honest lower bound, says unnamed sessions are not
  addressable, and tells the user to restart each intended terminal with a
  distinct `ANTIPHON_NAME`;
- a named send remains exact-or-refuse and never falls through to an
  observation or the legacy target;
- with one observed unnamed session and no named peer, the shipped legacy bare
  fallback remains, including its pre-first-turn `no-peer` refusal;
- with no observation the shipped behavior remains. Absence of evidence does
  not become evidence of uniqueness.

Preserving the single-peer fallback is an explicit compatibility choice. B1
eliminates known wrong-recipient choices; it cannot eliminate an ambiguity the
host exposes through no stable project-scoped observable. B2 is where that
remaining guarantee can change.

## 5. Diagnostics and documentation

`status` and `doctor` share one observation snapshot and one liveness
classifier, so they cannot disagree about the same moment. A stale or malformed
observation is ignored or named as unreadable according to the selected branch;
it is never counted as live by age alone. Reads must preserve current project
scoping and never enumerate another project's ids or paths.

README, `AGENTS_RULE`, `CLAUDE_RULE` and channel instructions continue to agree:
unnamed peers are unaddressable; multiple observed Codex candidates make a bare
send refuse; `ANTIPHON_NAME` is the supported remedy; a displayed host session
id is diagnostic identity, not a recipient alias.

## 6. Verification gate after the measurement approval

- Red-before-green tests for valid/malformed unnamed observations, atomic
  isolation, named-session suppression, liveness and stale records.
- Routing tests for two unnamed candidates, named-plus-unnamed, exact named
  delivery, single unnamed compatibility, no observation, and unknown census.
- Status/doctor tests for lower-bound wording, id visibility only on local
  diagnostic surfaces, no paths/content, stable ordering and project scoping.
- Mixed-version and missing-feature tests that say unknown rather than zero.
- Contract tests over README and all three agent instruction surfaces.
- Full Python/Node suite, static checks and fresh-user E2E on one exact clean
  commit because startup, registration and delivery are touched.
- Read-only Claude confirmation and the existing independent Codex reviewer on
  that exact SHA.

No live state mutation, push, merge, version bump or publish belongs to this
wave.
