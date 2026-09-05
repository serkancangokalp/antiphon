# Changelog

## 1.0.0

This release makes the
Claude Code / Codex bridge easier to start and makes uncertain outcomes
explicit instead of inviting duplicate work or premature cleanup.

### Everyday use

- `antiphon launch claude|codex` checks project readiness before starting the
  host, adds Claude's channel flag, and supports explicit peer names.
- Passive records from another day carry their date; an unrepresentable
  timestamp cannot crash a whole page. No guessed relative age is added.
  Today's records use `[HH:MM]`, other days `[YYYY-MM-DD HH:MM]`, and an
  unrepresentable timestamp says `time unavailable`.
- Explicit Codex host-content metadata keeps injected skill instructions and
  writing-block bookkeeping out of human speech; mixed or unknown kinds remain
  visible rather than being guessed away.
- Quick-start and upgrade instructions distinguish transport acceptance from
  a peer's receipt, and explain which sessions need restarting.

### Delivery and managed work

- Preserve early receipts across the send/receive race. Unresolved receipt
  writes hold the read cursor; bounded loss and expiry are diagnosable.
- Prepare handed tasks before transport and retain evidence after uncertain
  delivery/tracking outcomes. Callers are told not to retry automatically.
  `handing` means a hand-off is being prepared; `tracking_incomplete` retains
  uncertain local tracking after transport. Delivery-ledger `unknown` means
  transport may have happened without a trustworthy acknowledgement; a later
  receipt can resolve it.
- Managed workers use authenticated supervisor publication, conservative
  process-group observation, ordered stop intent and Git recovery evidence.
  Transient activity in unrelated groups no longer poisons liveness checks.
  `outcome_unknown` means the process group is proved dead but no authenticated
  outcome survived; missing liveness proof instead keeps a task `running`
  with its work and slot retained.
- The Node `antiphon_task` tool validates status/result/cancel responses, exact identifiers and
  cross-runtime limits. Invalid or truncated success output is an unknown
  outcome, not proof that no action occurred.
- Doctor names uncertain tasks; documented recovery preserves work and never
  presents a force-reclaim operation as safe.

### Release engineering

- Refresh the dependency lock to `qs` 6.16.0, which fixes
  [GHSA-x5fp-wj9c-mxmx](https://github.com/advisories/GHSA-x5fp-wj9c-mxmx)
  and [GHSA-4mjr-xmp4-gh2g](https://github.com/advisories/GHSA-4mjr-xmp4-gh2g).
- Claude discovery and the privacy-bounded host-wrapper census share the
  same candidate scope, including hidden and empty immediate JSONL files.
- CI preserves failed suite output as short-lived artifacts without masking
  the exit code. Packed-install tests exercise the shipped launcher outside
  the repository.

### Upgrading from 0.5.x

Finish and collect old managed work before upgrading. Install the new version,
run `antiphon setup` in each project, restart both host sessions, and check
`antiphon doctor`. Old worker rows remain listable but cannot be collected or
cancelled by the new protocol. Current workers use `.antiphon/tasks-v2/`.
The previous `.antiphon/tasks/` store must be quiescent before its transition
to a read-only `0500` fence; each new admission verifies that writes really
are refused. An old server does not gain the new protocol by changing files
on disk: restart both hosts after installation and setup.

Developer `npm link` installations need `npm link` again: the executable moved
from `bin/antiphon.mjs` to `bin/antiphon`. Ordinary npm installs relink it.

### Boundaries that remain

Claude's channel is a host research preview. Antiphon cannot guarantee host
availability or model obedience, and transport acceptance is not a receipt.
Unprovable lifecycle state retains evidence and can require operator recovery.
Managed work never merges itself. Same-user processes deliberately bypassing
the documented cooperative file/sandbox boundary are not made trustworthy by
the bridge. See README for the exact guarantees and recovery procedure.
