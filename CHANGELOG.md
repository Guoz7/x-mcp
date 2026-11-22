# Changelog

This project is currently in the `0.10.x` pre-release line. Entries describe
implemented repository changes, not a promise that every integration is ready
for public hosted use.

## Unreleased

- Upgraded interaction orchestration and security hardening:
  - Switched reply generation (`draft_friendly_reply` + `tech_casual` preset) to a relaxed, anti-AI casual-first voice (`vent_rant`, `social_chatter`, `milestone`, `creative`, `language_learning`, and peer `tech_discussion`), added `clean_post_body` to strip X card headers/metrics before classifying intent, enforced fail-closed skipping (`None`) for ambiguous or ultra-short fragments (such as 2-char replies like `"咋了"`), and fixed English anchor leakage into Japanese/Korean replies.
  - Raised growth-stage engagement capacity for likes (default `120/day`, up to `150/day`) and replies (default `45/day`, up to `50/day` at `75-145s` pacing with ~26 distinct EN/ZH variants), while tightening `repost_post` (`<= 8/day` cap, `4/day` autopilot default, `<= 1/wave`) through `score_technical_quality` so social chatter/followback bait is rejected and only substantive engineering posts qualify.
  - Added `interaction_strategy` (`reciprocal_first`, `reply_then_follow`, `balanced`) with per-author `visit -> like -> reply -> follow` bundling and engagement-rich micro-wave interleaving in `build_daily_orchestration_plan`.
  - Classified notifications (`follow`, `reply`, `like`, `repost`, `mention`) in `read_notifications`, added `record_mutual_followers` for hash-chain-safe mutual-follow reconciliation, decoupled periodic notification sync from `x_target_pool` wave consumption, and surfaced inbound/bundled telemetry in `ui/dashboard.html`.
  - Hardened local HTTP `Origin` validation to enforce same-port loopback matching, added `X-Frame-Options: DENY` and `X-Content-Type-Options: nosniff` headers, and sanitized public documentation, examples, and test fixtures.

- Reorganized the repository and refreshed documentation: removed obsolete
  October 2026 HTML dumps and `firefox_only_e2e.py`, archived dated
  incident and verification reports under `docs/reports/`, untracked the
  legacy `0.9.1` binary ZIP in `dist/`, sanitized developer-local
  paths in tracked files, updated `README.md` (English) and
  `README.zh-CN.md` (Simplified Chinese), and removed duplicate
  `x_target_pool` and `x_persona_memory` entries from
  `tools/list` (65 unique tools).

- Added Agent-driven personal account orchestration and safety guards:
  `x_account_health`, `x_content_queue`, `x_target_pool`,
  `x_persona_memory` (including the `tech_casual` preset and
  throttled `sync_owner`), `x_visit_profile`,
  `x_notifications_read`, `x_daily_orchestrate_*`, and
  `x_autopilot` (24-hour confirmation TTL, follow-heavy micro-waves with
  32-68s pacing, deduplicated cautious reply guards at 90-165s pacing, 2-hour
  security cooldown, and 3-day non-mutual cleanup).

- Redesigned the local Web Console (`ui/dashboard.html`, launched via
  `dashboard.cmd`) into an Obsidian-Zinc observer & lightweight approval UI
  where the Agent handles configuration and scheduling while the owner reviews
  prepared windows, live status, and exported reports.

- Generalized the operator window from follows to explicit write types.
  --actions names the allowed types and stores them in `action_types`.
  A follow-only list keeps the older `bounded_follow_run` mode. A window
  never admits a type that it does not name. Live checks on 2026-11-07
  completed 2 likes, 1 repost and 2 follows, each with a verified changed
  state. Forty-one focused checks pass.
  See [operator window](docs/reports/OPERATOR-WINDOW-2026-11-05.md).

- Fixed the bounded-window last-slot defect. A claim spent its slot before
  the action ran, so the action's own re-check rejected it and the final
  follow of every window failed against our counter. The window now records
  claimed action ids and admits only those at the limit. Thirteen focused
  checks pass, and two live waves verified their final action.
  See [operator window](docs/reports/OPERATOR-WINDOW-2026-11-05.md).

- Added a bounded owner-authorized follow window and a single-target
  calibration permit. They keep historical rate-limit evidence and revoke on
  a newer 429 or security interstitial. Sixty focused offline checks pass.
  See [operator window](docs/reports/OPERATOR-WINDOW-2026-11-05.md).

- Browser ledger transactions now use a reentrant cross-process file lock.
  Initialization uses the same lock, and the internal test archive includes
  the lock module. This does not clear historical rate stops or establish
  daily action capacity. See [verification](docs/reports/LEDGER-LOCK-2026-11-04.md).

- Independent repost/reply waves no longer require a like. Reposts no longer
  repeat liked targets when the requested repost count exceeds the like count.
  MCP validation and bridge preparation support zero-like nonempty waves.
  Forty-four focused offline checks pass; live daily targets remain blocked
  by the original headerless 429. See [daily evidence](docs/reports/DAILY-ACTIONS-2026-11-03.md).

- Added a read-only visual-diagnosis result validator. A real uncertain-profile
  check found a Follow button without retrying the write. The initial model
  eligibility hallucination is preserved as a failed task, despite exit 0.
  See [evidence](docs/reports/VISUAL-RECONCILIATION-2026-11-04.md).

- Corrected ledger-derived pacing for global stops, undated rate records,
  overlapping/conflicting cooldowns, uncertainty and unchanged results. Forty-six focused
  offline checks pass. Real External Agent image admission and pixel inspection now work
  on the tested local route; no daily 200-per-category capacity claim is made.
  See [evidence](docs/reports/AGENT-VISUAL-READINESS-2026-11-04.md).

- Added a read-only follow evidence audit with strict unique counts,
  hash-chain comparisons, explicit timing sources and visual-handoff gaps.
  A real External Agent screenshot check exposed route-level image admission blocking;
  see [audit evidence](docs/reports/FOLLOW-EVIDENCE-AUDIT-2026-11-03.md).

- Added separate daily verified-action accounting for follows, likes, reposts
  and replies (target 200 each), without installing a recurring job.
- Interaction batches now stop on observed 429/security/global faults, bound
  action counts, and validate scoped confirmations on resume. Likes/reposts
  verify after reload. See [evidence and limits](docs/reports/DAILY-ACTIONS-2026-11-03.md).
- Fixed owner display-name parsing to use the profile header instead of a
  keyboard-shortcut tooltip at the start of page text.

- Recorded X rate limits now block subsequent write claims and grants across
  batches/sessions/restarts in the same account ledger. A known Retry-After
  expires by its recorded completion time; missing recovery times fail closed.
- Added a pure evidence-driven wave policy for cooldown, calibration, reset and
  manual recovery decisions. It does not send requests or retry actions.
- Follow batches now stop on global browser/security/rate-limit errors even
  under the continue policy. Error records retain attempt timing.
  See [focused and partial live evidence](docs/reports/FOLLOW-GUARDS-2026-11-03.md).
- HTTP 429 responses halt the batch and preserve numeric Retry-After values.
  Post-write visual handoffs are read-only and preserve sanitized API evidence;
  they never instruct another follow click after an unknown write outcome.
- Fixed `x_follow_batch_execute`: the success path referenced an undefined
  `browser_family`, so every verified follow was recorded as `uncertain` and the
  default `stop` policy ended the batch after the first target. Added a
  regression test with a fake browser.

## 0.10.0 - mutual-follow batch foundation

- Added visible mutual-follow candidate discovery without credential or storage access.
- Added confirmed follow planning, per-target leases, reload verification, status recovery, and uncertain-stop behavior.
- Added package, manifest, public skill, prompt, and performance documentation for follow batches.
- A live 200-follow result remains unverified.

## 0.9.1 - internal validation

- Interaction batches now accept up to 200 likes, 10 reposts and 200 replies in
  one plan. Each plan is split into numbered chunks so a chunk can be
  confirmed, executed and resumed on its own.
- The same confirmed batch can run through the built-in browser executor or
  through an external agent that claims one action at a time with
  `x_browser_next_action` and reports back with `x_browser_complete_action`.
- `x_interaction_batch_status` reports per-chunk progress so a long job can be
  inspected without holding an MCP request open.

- Added per-MCP-session browser profiles and lifecycle cleanup.
- Added claim-gated browser writes with observation-bound evidence.
- Added owner login and read checks that never handle passwords or tokens.
- Added bounded interaction batches for likes, reposts, and replies.
- Added credential-free public X reads and an optional Agent Help board.
- Added source-package smoke tests and a credential-free internal tester ZIP.
- Added a plain-language promotion kit, public performance snapshot, and a
  synthetic-account narrated demo source.

Known gaps are tracked in `docs/RELEASE_READINESS.md`.
