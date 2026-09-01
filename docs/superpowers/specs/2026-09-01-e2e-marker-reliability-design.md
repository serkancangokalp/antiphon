# Fresh-User Exact-Marker Reliability Design

**Date:** 2026-09-01

**Scope:** Make the real-CLI fresh-user gate tolerate bounded model wording
non-determinism without retrying product behavior, accepting an approximate
answer, or losing the evidence needed to diagnose a final failure.

## 1. Problem and measured boundary

On exact commit `802fc8a`, the first fresh-user run passed 91 of 93 assertions.
The first failing assertion could not find the exact Claude-authored marker;
the second was the downstream assertion for that same marker. Two later runs,
each with a fresh temporary project and nonce, passed 93 of 93. The product
diff was confined to compaction and could not reach the T2/T3 hook and push
path. The varying component was the live `claude -p` response.

Repeating the whole stage is not acceptable. It would re-run push, queue and
page behavior and could hide a real regression. Fabricating a transcript is
also not acceptable: T2 exists to prove behavior against a real Claude
transcript.

The existing `grep -qr` precondition is weaker than it reads. The prompt itself
contains the marker, so it can pass even when no assistant block contains the
requested line. The retry predicate must inspect assistant text blocks and
require the exact one-line response.

## 2. Contract

- Each of the two marker-producing `claude -p` turns gets at most three
  attempts.
- A zero CLI exit with no exact assistant text block is the only retryable
  outcome. A nonzero CLI exit is a distinct failure and is not retried.
- The accepted assistant text is exactly `@codex <per-run nonce>-<suffix>`
  after trimming its surrounding whitespace. A prompt record, prefixed prose,
  code fence or approximate wording does not satisfy it.
- Success prints which attempt landed. The fluctuation remains observable.
- The exact transcript that contains the second accepted marker becomes the
  T2 push input. The test does not trust "newest" after it has stronger
  evidence.
- Push, queue, rollout discovery, page delivery and T3 are each executed once.
  Nothing retries T2/T3 wholesale.
- If all three attempts omit the marker, the run fails and preserves the
  temporary project plus Claude transcript directory automatically. It says
  why they were preserved. The ordinary successful cleanup and explicit
  `--keep` behavior remain unchanged.
- The marker probe prints only a matched temporary transcript path. It never
  prints prompt or assistant content.

## 3. Test seam

A small harness-only Python probe under `test/e2e/` reads completed JSONL
records and reports the newest transcript containing the exact assistant text
block. It is not included in the npm package; the fresh-user script itself is
also a repository-side release gate rather than package runtime.

Unit tests create throwaway transcripts and prove:

- a user prompt containing the marker cannot pass;
- an exact assistant text block passes;
- a preamble, suffix or fenced form does not pass;
- malformed and partial records do not pass or raise;
- the newest exact matching transcript is selected without printing content.

The shell harness then uses that probe in its bounded loop. Red is observed
first against the existing prompt-matching `grep` behavior.

## 4. Failure evidence and provenance

Default commit mode still refuses a dirty tree, packs `git archive <SHA>`,
re-checks HEAD and cleanliness, and prints the exact tested SHA. The probe is
part of that clean reviewed tree. Published-version mode remains explicitly
commitless.

Only the final marker-landing failure changes cleanup policy. Preflight
refusals and successful runs do not begin accumulating temporary trees.

## 5. Verification gate

- Focused probe unit tests and `bash -n test/e2e/fresh-user.sh`.
- Full `npm test` and static checks.
- A clean exact commit.
- One real fresh-user run on that exact commit, with the landing attempt shown.
- Read-only Claude contract confirmation and the existing independent Codex
  reviewer on that SHA.

No product runtime, push, merge, version bump or publish belongs to this wave.
