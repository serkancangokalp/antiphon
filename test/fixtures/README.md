# Frozen readers

`peers_0_3_3.py` is `lib/peers.py` exactly as commit `943da8a` (npm 0.3.3)
shipped it — the same bytes as `a076723` — and the last reader that
interpreted an endpoint's `birth` with no generation marker. It is imported
only by tests, never by product code, and `FrozenReaderFixtureTest` refuses
any byte that differs from those blobs. It exists because a rolling upgrade
leaves that reader running for hours inside a live MCP server, and a test
that models it instead of running it proved nothing. `package.json` `files`
excludes `test/`, so none of this ships.

`mixed_lib.mjs` is a helper, not a fixture file: it assembles a `lib/` whose
Node files and Python files come from independently chosen sources (a commit
or the working tree) and can swap the Python files afterwards. That is what a
running listener sees across an upgrade or a downgrade on disk: the Node it
loaded stays in memory, the Python it shells is whatever is on disk now.
