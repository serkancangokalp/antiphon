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
  let bytes;
  try {
    const size = statSync(path).size;
    if (size > RECORD_CEILING) return { state: "invalid" };
    bytes = readFileSync(path);
  } catch (error) {
    return { state: error?.code === "ENOENT" ? "absent" : "unreadable" };
  }
  let raw;
  try {
    // A decode that refuses rather than repairs: reading with an encoding
    // substitutes U+FFFD for an undecodable byte and carries on, while
    // Python's reader refuses the bytes. Kept in its own arm because the
    // fatal decoder throws with a `code` of its own — classified beside the
    // I/O errors it would have read as `unreadable`, which is the answer for
    // "nothing could be learned", not for "a record is there and is torn".
    raw = FATAL_UTF8.decode(bytes);
  } catch {
    return { state: "invalid" };
  }
  try {
    // A name declared twice is not a record. `JSON.parse` keeps the last
    // silently, and Python's reader refuses the whole object — so a record
    // could read one way here and another way there with both parsers calling
    // it valid. The parsed object cannot show it; the source can.
    const record = JSON.parse(raw);
    const scan = scanRecord(raw);
    if (scan.duplicate) return { state: "invalid" };
    // The raw text travels with the record. JSON has one number type and
    // `JSON.parse('{"version":1.0}')` yields the same `1` an integer does, so
    // nothing about the parsed value can tell them apart — while Python's
    // `isinstance(version, int)` rejects the float. Without the source text
    // the two readers disagree in the worst direction: Python refuses the
    // record and Node calls it READY.
    return { state: "valid", record, scan };
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

// Every record this reader touches is a handful of fields. A ceiling costs
// nothing and keeps an unbounded read off the inbound-delivery path, where
// everything else in this repair already carries one.
const RECORD_CEILING = 64 * 1024;

// `PID_CEILING` in `peers.py`, mirrored: above the platform's signed int
// `os.kill` refuses to answer at all, so a larger number names no process on
// either side.
// `BLANK` in `peers.py`, mirrored: `trim()` and `strip()` disagree in both
// directions, so the set is spelled rather than inherited from either.
const BLANK = /[ \t\n\r\f\v\u001c-\u001f\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]/g;

const PID_CEILING = 2 ** 31 - 1;

// Refuses an undecodable byte instead of substituting U+FFFD for it, and keeps
// a leading byte-order mark instead of eating it. `ignoreBOM` defaults to
// false, which *strips* the mark — so a record with a BOM parsed cleanly here
// and was refused by Python, which keeps the character and lets `json` reject
// it. The name reads backwards: `ignoreBOM: true` means "treat it as ordinary
// data", which is what a reader that must agree with another parser wants.
const FATAL_UTF8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });

// One lexical pass over the source, answering the two questions the parsed
// value cannot. Both were tried as patterns first and both were wrong for the
// same reason: a JSON key may be spelled `"pid"` or `"p\u0069d"`, and a match
// on text has no idea which object level it is looking at.
//
// - **Duplicates, at any depth, under any spelling.** `JSON.parse` keeps the
//   last and says nothing; Python's reader refuses the whole record. Names are
//   decoded before they are compared.
// - **Whether a field was written as an integer.** JSON has one number type,
//   so `1.0` and `4242.0` parse to the very integers `1` and `4242` — nothing
//   about the parsed value tells them from the real thing, while Python's
//   `isinstance(x, int)` refuses both. Scoped to the record's own fields, so
//   an unrelated nested `1.0` is not this record's business.
function scanRecord(raw) {
  const containers = [];
  const names = [];
  const integral = new Map();
  let duplicate = false;
  let inString = false;
  let escaped = false;
  let expectKey = false;
  let text = "";
  let pending = null;
  let i = 0;
  while (i < raw.length) {
    const ch = raw[i];
    if (inString) {
      if (escaped) { text += ch; escaped = false; i += 1; continue; }
      if (ch === "\\") { text += ch; escaped = true; i += 1; continue; }
      if (ch === '"') {
        inString = false;
        if (expectKey && containers[containers.length - 1] === "object") {
          let name;
          try { name = JSON.parse(`"${text}"`); } catch { return { duplicate: true, integral }; }
          const seen = names[names.length - 1];
          if (seen.has(name)) duplicate = true;
          seen.add(name);
          expectKey = false;
          pending = containers.length === 1 ? name : null;
        } else {
          pending = null;
        }
        i += 1; continue;
      }
      text += ch; i += 1; continue;
    }
    if (ch === '"') { inString = true; text = ""; i += 1; continue; }
    if (ch === "{") {
      containers.push("object"); names.push(new Set());
      expectKey = true; pending = null; i += 1; continue;
    }
    if (ch === "[") { containers.push("array"); pending = null; i += 1; continue; }
    if (ch === "}") { containers.pop(); names.pop(); expectKey = false; pending = null; i += 1; continue; }
    if (ch === "]") { containers.pop(); expectKey = false; pending = null; i += 1; continue; }
    if (ch === ",") {
      if (containers[containers.length - 1] === "object") expectKey = true;
      pending = null; i += 1; continue;
    }
    if (pending !== null && (ch === "-" || (ch >= "0" && ch <= "9"))) {
      let j = i;
      while (j < raw.length && /[-+0-9.eE]/.test(raw[j])) j += 1;
      integral.set(pending, /^-?(?:0|[1-9][0-9]*)$/.test(raw.slice(i, j)));
      pending = null; i = j; continue;
    }
    i += 1;
  }
  return { duplicate, integral };
}

// True when this record spells the named field as anything but an integer.
function spelledFractional(record, field) {
  return record.scan?.integral.get(field) === false;
}

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
    && !spelledFractional(retired, "version")
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
      || spelledFractional(proof, "version")
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

export function automaticProofVerdict(projectDir, peerId, identityDigest,
                                      listenerPid, listenerBirth) {
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
  // And the pid has to be this listener's. Checking only the type let a record
  // naming a dead or unrelated process read as this listener's own endpoint —
  // one the Python resolver prunes before it can be enumerated at all, so this
  // side answered READY, and with a rotation tombstone beside it, retired.
  if (endpoint.record?.name !== peerId
      || endpoint.record?.kind !== "claude"
      || !Number.isInteger(pid) || pid <= 0 || pid > PID_CEILING
      || spelledFractional(endpoint, "pid")
      || (Number.isInteger(listenerPid) && pid !== listenerPid)
      // The birth this listener's own claim wrote. A record naming this pid
      // with another birth is an earlier process's, left behind and matched
      // only by a recycled number — which is the pairing `owner_key` has
      // always insisted on, and the one Python prunes on.
      || (typeof listenerBirth === "string"
          && endpoint.record?.birth !== listenerBirth)
      || typeof endpoint.record?.address !== "string"
      || !endpoint.record.address.replace(BLANK, "")) {
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
// The boundary belongs to the shape, not to the alphabet — see `peers`' twin.
const PRIVATE_DIGEST = /(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])/g;
const PRIVATE_OWNER =
  /(?<!\d)\d+:(?:v\d+:)?[A-Z][a-z]{2} [A-Z][a-z]{2} [ \d]?\d \d{2}:\d{2}:\d{2} \d{4}/g;
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
