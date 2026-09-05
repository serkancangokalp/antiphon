# CI portability repair evidence

Base: `afbb77c57e2a75f499dc5fdf01a76340fb478663`.
Work branch: `codex/ci-portability`; main, tags and npm remain untouched.
The approved order is B (stdio lifecycle), A (CLI-independent test fixtures),
then C (Linux process-table evidence and any justified worker correction).
Only codex branch pushes and CI dispatch are authorized at this stage.

## B — early stdin EOF

The SDK starts consuming stdin in `connect`. The previous channel then awaited
filesystem startup before installing its EOF handlers. A real `end` and `close`
in that interval were lost, and startup bound a socket after the client left.
This was reproduced with Node 20.20.2 and Node 26.7.0 on macOS; it is not a
Node-20-only incompatibility.

Shutdown's prerequisites and handlers now precede `connect`; mkdir follows it.
An already-ended/destroyed stream also requests shutdown. Startup observes that
request before claiming or binding, then settles the existing identity barrier
so shutdown can finish its serialized release. No new production test hook or
runtime dependency was added.

Evidence, full logs under `/tmp/antiphon-ci-b-*.log`:

- RED: a test-only preloader holds real mkdir, observes both EOF events without
  adding an EOF handler, then releases startup. The original channel remains
  alive and announces a socket; the test fails on the required clean exit.
- GREEN: both during-mkdir and already-ended cases pass on Node 20.20.2 and
  26.7.0. Exit is 0 without a signal; no socket or endpoint remains, and no
  ready announcement is made. The initialize response is retained.
- Mutation: disabling only the already-ended state check leaves during-mkdir
  green and makes already-ended fail. Restored channel SHA-256:
  `979dbdc48e7b74443267185b304835cb666d7e325fa56ede8a11bdbc4677d126`.
- Python 3.9 full suite: 1,586 tests, OK, 3 named skips; contracts 61, CI helpers
  20, full Node suite exit 0. Syntax and whitespace checks pass.
- Baseline Node run hit an existing test-helper check/read race while an
  endpoint was being withdrawn (`registeredPeers` ENOENT); the subsequent
  full run passed. This is separate from B and belongs to fixture slice A.

Local green is not hosted-CI green. Run 33968789080 remains the known failing
baseline. A and C, an independent exact-SHA review, and a new hosted matrix
are still required before integration or a 1.1.0 release candidate.
