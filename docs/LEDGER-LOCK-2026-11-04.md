# Browser ledger process lock

## Scope

BrowserBridge previously used a process-local RLock. Two server processes
could therefore read and claim the same pending action before atomic file
replacement. LedgerLock now combines a reentrant thread lock with a byte
lock on Windows or flock on POSIX. Initialization and existing bridge
transaction blocks use this lock. The internal archive includes the module.

No daily quota enforcement, scheduler, rate recovery override or visual write
recovery was added. Historical 429 evidence and the existing gate stay intact.
Private helpers that directly call _write must still hold the transaction
lock. This protects the local ledger, not different ledgers or remote hosts.

## Focused verification

Command from the repository root:

```powershell
python -X utf8 -m unittest tests.test_ledger_lock tests.test_rate_gate_clock -v
```

Exit 0: eight checks, 1.244 seconds, OK. The subprocess fixture could not
enter while the parent held the ledger lock and entered after release.
Nested acquisition and six existing rate-gate checks also passed.
The fixtures use temporary directories, no X requests and no external writes.

Evidence in the parent workspace: releases/ledger-lock-focused-20261104.log.
No whole-repository regression was run. No project /code-review entry was
found; the main agent reviewed the diff and checked archive inclusion.

## Live observation, not write acceptance

A read-only profile scan found a fresh candidate with a Follow button and
visible advertised mutual-follow text. It performed zero follows. The prior
DSH readback showed Following for the owner's manually selected profile.
Neither observation establishes bulk write recovery or daily capacity.

The original account ledger remained byte-for-byte unchanged:
SHA-256 c31fbeaad22e9346f6332ae5f8560d471bf5ad5e2fb5b6d00c2bdd76d59268a7.
Strict unique verified new follows remain 104, not 200. This turn made zero
new write attempts. A draft calibration override was discarded before it
could touch the real ledger; it is not part of the implementation.
