// The automatic-Claude readiness verdict, as Node sees it.
//
// One file format, two readers. Node cannot call a Python function, and the
// check runs on every inbound delivery, so a subprocess per message would be
// both a cost and a new failure mode on the hot path. The two implementations
// are therefore mirrored and their agreement is enforced by a parity suite
// rather than asserted here.
//
// The answers stay apart on purpose. A boolean would cover a listener whose
// proof has not been written yet and one whose proof now names another
// session with the same false, and only the second may be retired.

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const CANONICAL_UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const PROOF_VERSION = 1;

function readRecord(path) {
  // Absent, unreadable and unparseable are three facts, never one null.
  let raw;
  try {
    raw = readFileSync(path, "utf8");
  } catch (error) {
    return { state: error?.code === "ENOENT" ? "absent" : "unreadable" };
  }
  try {
    return { state: "valid", record: JSON.parse(raw) };
  } catch {
    return { state: "invalid" };
  }
}

// The session id these two halves agree on, or null. They join on the owner
// key and nothing else, exactly as the Python reader does; reaching for the
// likeliest session is the misrouting this whole contract exists to end.
function sessionAddress(root) {
  const endpoint = readRecord(join(root, "endpoint.json"));
  if (endpoint.state !== "valid") return null;
  const session = readRecord(join(root, "session.json"));
  if (session.state !== "valid") return null;
  const owner = endpoint.record?.owner;
  if (typeof owner !== "string" || !owner) return null;
  if (session.record?.owner !== owner) return null;
  const id = session.record?.session_id;
  return typeof id === "string" && CANONICAL_UUID.test(id) ? id : null;
}

export function automaticProofVerdict(projectDir, peerId, identityDigest) {
  if (!identityDigest) return null;            // ungoverned: an explicit peer
  const root = join(projectDir, ".antiphon", "peers", `claude-${peerId}`);
  const endpoint = readRecord(join(root, "endpoint.json"));
  if (endpoint.state !== "valid") return "UNREADY";
  const owner = endpoint.record?.owner;
  if (typeof owner !== "string" || !owner) return "UNREADY";
  if (endpoint.record?.automatic !== true
      || endpoint.record?.identity_digest !== identityDigest) {
    return null;                               // not this contract's record
  }

  const ownerDigest = createHash("sha256").update(owner).digest("hex");
  const proof = readRecord(join(projectDir, ".antiphon", "identity", "claude",
                                `${ownerDigest}.json`));
  if (proof.state === "absent") return "UNREADY";
  if (proof.state === "unreadable") return "UNKNOWN";
  if (proof.state === "invalid") return "STRUCTURAL_INVALID";

  const record = proof.record;
  const sessionId = record?.session_id;
  if (record?.version !== PROOF_VERSION
      || typeof record?.version !== "number"
      || record?.kind !== "claude"
      || record?.owner_digest !== ownerDigest
      || record?.owner_key !== owner
      || typeof sessionId !== "string"
      || !CANONICAL_UUID.test(sessionId)
      || record?.identity_digest
         !== createHash("sha256").update(sessionId).digest("hex")) {
    return "STRUCTURAL_INVALID";
  }

  // Not joined yet is not stale: the next hook is about to make it ready.
  const bound = sessionAddress(root);
  if (bound === null) return "UNREADY";
  return bound === sessionId && record.identity_digest === identityDigest
    ? "READY"
    : "PROVED_STALE";
}

// ---- privacy -------------------------------------------------------------
//
// The second thing this file mirrors, and for the same reason: the channel
// prints its own refusals and never crosses back into Python to have them
// cleaned. A shape only the Python redactor removes still reaches a terminal
// from here. Their agreement is enforced by the same kind of parity suite.
//
// Applied *before* truncation, always. A cut taken first can leave half a
// session id behind, and a check that looks for a whole one would then pass
// over the fragment. The public `auto-` alias is deliberately not a private
// shape — it is the useful half of every message here, and the remedy beside
// it is what a person acts on.
const PRIVATE_UUID =
  /[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/g;
const PRIVATE_DIGEST = /\b[0-9a-fA-F]{64}\b/g;
const PRIVATE_OWNER =
  /\b\d+:(?:v\d+:)?[A-Z][a-z]{2} [A-Z][a-z]{2} [ \d]?\d \d{2}:\d{2}:\d{2} \d{4}/g;
const PRIVATE_ROUTE = /\S*antiphon-channel-[0-9a-f]+\.sock/g;

export function redactPrivate(text, limit = null) {
  if (typeof text !== "string") return text;
  const cleaned = text
    .replace(PRIVATE_ROUTE, "<route>")
    .replace(PRIVATE_OWNER, "<owner>")
    .replace(PRIVATE_DIGEST, "<digest>")
    .replace(PRIVATE_UUID, "<session>");
  return limit === null ? cleaned : cleaned.slice(0, limit);
}
