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
import { readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const CANONICAL_UUID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const PROOF_VERSION = 1;

function readRecord(path) {
  // Absent, unreadable and unparseable are three facts, never one null.
  let raw;
  try {
    const size = statSync(path).size;
    if (size > RECORD_CEILING) return { state: "invalid" };
    raw = readFileSync(path, "utf8");
  } catch (error) {
    return { state: error?.code === "ENOENT" ? "absent" : "unreadable" };
  }
  try {
    // The raw text travels with the record. JSON has one number type and
    // `JSON.parse('{"version":1.0}')` yields the same `1` an integer does, so
    // nothing about the parsed value can tell them apart — while Python's
    // `isinstance(version, int)` rejects the float. Without the source text
    // the two readers disagree in the worst direction: Python refuses the
    // record and Node calls it READY.
    return { state: "valid", record: JSON.parse(raw), raw };
  } catch {
    return { state: "invalid" };
  }
}

// `[1-9][0-9]*:` then a non-empty, non-padded remainder — `OWNER_PATTERN` in
// `peers.py`, mirrored. Without it Node accepted any non-empty string as an
// owner and reported READY for a proof Python refuses outright.
// The mirror of `OWNER_PATTERN`, spelled rather than inherited. `.` in Python
// is not DOTALL, so a key spanning a newline is refused there — and `\S` is a
// different set in each language, in both directions. Both sides refuse the
// union: `\u001c`-`\u001f` and `\u0085` are whitespace to Python, `\ufeff` to
// JS, and a key holding any of them is now refused by both.
const OWNER_KEY =
  /^[1-9][0-9]*:[^\s\u001c-\u001f\u0085](?:[^\n]*[^\s\u001c-\u001f\u0085])?$/;

function validOwnerKey(key) {
  return typeof key === "string" && OWNER_KEY.test(key);
}

// The `version` key's literal token, read from the source rather than the
// parsed value, so a float spelling is refused exactly where Python refuses it.
// Stated as "no float anywhere" rather than "an integer somewhere": the second
// passes on a record that spells its own version `1.0` and happens to carry the
// literal `"version": 1` inside another value.
// JSON has one number type, so `1.0` and `4242.0` parse to the very integers
// `1` and `4242` — nothing about the parsed value can tell them from the real
// thing, while Python's `isinstance(x, int)` refuses both. The spelling is only
// visible in the source text. Stated as "no float anywhere" rather than "an
// integer somewhere": the second passes on a record that spells its own field
// as a float and happens to carry an integral literal inside another value.
const FLOAT_NUMBER = /"(?:version|pid)"\s*:\s*-?\d+(?:\.\d+|[eE][-+]?\d+)/;

// Every record this reader touches is a handful of fields. A ceiling costs
// nothing and keeps an unbounded read off the inbound-delivery path, where
// everything else in this repair already carries one.
const RECORD_CEILING = 64 * 1024;

// The session id these two halves agree on, or null. They join on the owner
// key and nothing else, exactly as the Python reader does; reaching for the
// likeliest session is the misrouting this whole contract exists to end.
// `_identity_digest_of` and `_record_identity_valid`, mirrored. Without them
// Node joined the halves on owner alone, so a session half naming another
// identity — or carrying no digest at all, which is what a pre-upgrade half
// looks like — read READY here and UNREADY in Python. Task 10's own rule is
// that older or mixed-version records are never guessed into automatic
// identity, and this is where Node was guessing.
// The public alias one complete digest carries. Lives here rather than in
// `channel.mjs` because it is half of the identity binding both readers must
// agree on: Python derives the alias from the digest and requires the record's
// own `name` to match it, and a mirror without that check accepted a half
// whose name derives nothing — then reached PROVED_STALE on it and destroyed a
// listener Python was still telling to wait.
export function automaticNameFromDigest(digest) {
  const alphabet = "abcdefghijklmnopqrstuvwxyz234567";
  const bytes = Buffer.from(digest.slice(0, 32), "hex");
  let bits = 0;
  let value = 0;
  let encoded = "";
  for (const byte of bytes) {
    value = (value << 8) | byte;
    bits += 8;
    while (bits >= 5) {
      encoded += alphabet[(value >>> (bits - 5)) & 31];
      bits -= 5;
    }
    value &= bits ? (1 << bits) - 1 : 0;
  }
  if (bits) encoded += alphabet[(value << (5 - bits)) & 31];
  return `auto-${encoded}`;
}

function identityDigestOf(record) {
  if (record?.automatic !== true) return null;
  const digest = record?.identity_digest;
  if (typeof digest !== "string" || !/^[0-9a-f]{64}$/.test(digest)) return null;
  return automaticNameFromDigest(digest) === record?.name ? digest : null;
}

function recordIdentityValid(record, sessionDigest) {
  if (typeof record !== "object" || record === null) return false;
  const automatic = record.automatic;
  const digestPresent = Object.hasOwn(record, "identity_digest");
  if (automatic === undefined && !digestPresent) return true;
  if (automatic !== true) return false;
  const digest = identityDigestOf(record);
  if (digest === null) return false;
  if (Object.hasOwn(record, "session_id")) {
    // The id must derive the digest the record carries: one half cannot claim
    // an identity its own session id does not produce.
    return typeof record.session_id === "string"
      && CANONICAL_UUID.test(record.session_id)
      && sessionDigest(record.session_id) === digest;
  }
  return true;
}

function sessionAddress(root, sessionDigest) {
  const endpoint = readRecord(join(root, "endpoint.json"));
  if (endpoint.state !== "valid") return null;
  if (!recordIdentityValid(endpoint.record, sessionDigest)) return null;
  const session = readRecord(join(root, "session.json"));
  if (session.state !== "valid") return null;
  if (!recordIdentityValid(session.record, sessionDigest)) return null;
  const owner = endpoint.record?.owner;
  if (!validOwnerKey(owner)) return null;
  if (session.record?.owner !== owner) return null;
  if (identityDigestOf(endpoint.record) !== identityDigestOf(session.record)) {
    return null;
  }
  const id = session.record?.session_id;
  return typeof id === "string" && CANONICAL_UUID.test(id) ? id : null;
}

const RETIRED_HALF_VERSION = 1;

// Exactly these keys, on both sides. An ignored key is still a key somebody
// wrote, and a value can carry the very text the float scan reads.
const PROOF_KEYS = ["version", "kind", "owner_key", "owner_digest",
                    "session_id", "identity_digest"];
const RETIRED_KEYS = ["version", "kind", "owner", "identity_digest",
                      "session_id"];

function exactKeys(record, expected) {
  if (typeof record !== "object" || record === null) return false;
  const keys = Object.keys(record);
  return keys.length === expected.length
    && expected.every((key) => Object.hasOwn(record, key));
}

// Total, exactly as the Python reader is: a tombstone from another owner or
// another identity says nothing about this endpoint, and a record that cannot
// be trusted must never authorise the one destructive action in this contract.
function retiredHalf(root, owner, identityDigest, currentSessionId,
                    sessionDigest) {
  // Only genuine absence may consult the tombstone. A read that failed is
  // evidence of nothing and a torn record is evidence of corruption; neither
  // says this owner moved on, and only that may retire a listener.
  if (readRecord(join(root, "session.json")).state !== "absent") return false;
  const retired = readRecord(join(root, "retired.json"));
  if (retired.state !== "valid") return false;
  const record = retired.record;
  const withdrawn = record?.session_id;
  return exactKeys(record, RETIRED_KEYS)
    && record?.version === RETIRED_HALF_VERSION
    && Number.isInteger(record?.version)
    && !FLOAT_NUMBER.test(retired.raw || "")
    && record?.kind === "claude"
    && record?.owner === owner
    && validOwnerKey(owner)
    && record?.identity_digest === identityDigest
    && typeof withdrawn === "string"
    && CANONICAL_UUID.test(withdrawn)
    && sessionDigest(withdrawn) === identityDigest
    // The owner coming back to this identity is not a rotation.
    && typeof currentSessionId === "string"
    && CANONICAL_UUID.test(currentSessionId)
    && currentSessionId !== withdrawn;
}

const sessionDigest = (id) => createHash("sha256").update(id).digest("hex");

// One validator, both reads. The verdict reads the proof once up front and
// again after observing the halves, to close the window where a rotation lands
// in between — and the second read used to be parse-plus-session-id while the
// first was total. A proof that is malformed but carries the same session id
// could therefore authorise PROVED_STALE on this side and nowhere else. The
// divergence is not fixed by remembering to check the same things twice; it is
// fixed by there being one place that knows what a valid proof is.
function readIdentityProof(projectDir, owner, ownerDigest) {
  const proof = readRecord(join(projectDir, ".antiphon", "identity", "claude",
                                `${ownerDigest}.json`));
  if (proof.state !== "valid") return proof;
  const record = proof.record;
  const sessionId = record?.session_id;
  if (!exactKeys(record, PROOF_KEYS)
      || record?.version !== PROOF_VERSION
      || !Number.isInteger(record?.version)
      || FLOAT_NUMBER.test(proof.raw || "")
      || record?.kind !== "claude"
      || record?.owner_digest !== ownerDigest
      || record?.owner_key !== owner
      || typeof sessionId !== "string"
      || !CANONICAL_UUID.test(sessionId)
      || record?.identity_digest !== sessionDigest(sessionId)) {
    return { state: "invalid" };
  }
  return proof;
}

export function automaticProofVerdict(projectDir, peerId, identityDigest) {
  if (!identityDigest) return null;            // ungoverned: an explicit peer
  const root = join(projectDir, ".antiphon", "peers", `claude-${peerId}`);
  const endpoint = readRecord(join(root, "endpoint.json"));
  if (endpoint.state !== "valid") return "UNREADY";
  // What `read_peers` filters on before a record is a peer at all: the kind,
  // a usable pid and a non-empty address. Without these Node answered READY
  // for records Python cannot even enumerate.
  const pid = endpoint.record?.pid;
  // `_scan` binds a record to the directory it sits in; without the mirror an
  // endpoint whose `name` is not its own directory read as a peer here and as
  // nothing at all there — and with a rotation tombstone beside it, this
  // reader retired a listener over a record the other cannot enumerate.
  if (endpoint.record?.name !== peerId
      || endpoint.record?.kind !== "claude"
      || !Number.isInteger(pid) || pid <= 0
      || FLOAT_NUMBER.test(endpoint.raw || "")
      || typeof endpoint.record?.address !== "string"
      || !endpoint.record.address.trim()) {
    return "UNREADY";
  }
  // Governance is decided before anything else, exactly as Python decides it:
  // a record this contract does not govern returns `null` whatever its owner
  // key looks like. Asking about the owner first made an ungoverned record
  // answer UNREADY, which is a verdict about a record that has none.
  if (endpoint.record?.automatic !== true
      || endpoint.record?.identity_digest !== identityDigest) {
    return null;                               // not this contract's record
  }
  const owner = endpoint.record?.owner;
  // Python reaches its proof through `read_identity_proof`, which answers
  // `invalid` for an unusable key rather than looking for a file named from
  // one. A missing or non-canonical owner is therefore structural, not "not
  // ready yet".
  if (!validOwnerKey(owner)) return "STRUCTURAL_INVALID";

  const ownerDigest = createHash("sha256").update(owner).digest("hex");
  const proof = readIdentityProof(projectDir, owner, ownerDigest);
  if (proof.state !== "valid") {
    return proof.state === "absent" ? "UNREADY"
      : proof.state === "unreadable" ? "UNKNOWN"
      : "STRUCTURAL_INVALID";
  }
  const record = proof.record;
  const sessionId = record.session_id;

  // Not joined yet is not stale: the next hook is about to make it ready.
  // Withdrawn is a third thing, and it leaves no half either — so the
  // tombstone beside the halves is what keeps "waiting for a first hook" and
  // "outgrown by a rotation" apart. Only the second may retire anything.
  const bound = sessionAddress(root, sessionDigest);
  if (bound === null) {
    if (!retiredHalf(root, owner, identityDigest, sessionId, sessionDigest)) {
      return "UNREADY";
    }
    // The proof above was read before any of this observed the halves, and a
    // rotation can land in between. Re-read through the same validator and
    // require agreement: a snapshot that moved — or one that is no longer a
    // proof at all — is not one this may retire a listener on.
    const again = readIdentityProof(projectDir, owner, ownerDigest);
    return again.state === "valid" && again.record.session_id === sessionId
      ? "PROVED_STALE"
      : "UNREADY";
  }
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
