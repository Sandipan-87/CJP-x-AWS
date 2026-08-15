# Demo video plan + submission timeline

> Written 2026-08-15, during the final pre-submission rehearsal session. Deadline confirmed
> directly with the user against `CLAUDE.md §1`'s own stated date (there was a real discrepancy —
> user initially recalled the 17th; **confirmed correct: 2026-08-18, 17:00 ET, submit by 12:00 ET**).
> Judging criteria, five **equally weighted** (`docs/submission-checklist.md §0`): **Agentic Memory
> Design · Technological Implementation · Real-World Impact · Product Readiness · Creativity &
> Originality.**

## A real, unstarted gap found while planning this

**No root-level `README.md` exists in this repo, and no architecture diagram exists either**
(confirmed by direct search this session — only `dashboard/README.md`, `infra/README.md`,
`workers/README.md`, `db/migrations/README.md`, `db/target/README.md`; nothing at the repo root).
`docs/submission-checklist.md §3` requires the README to carry: quickstart · architecture diagram
· the four-identity + blast-radius tables · measured numbers (recall latency, beam-size
trade-off, MTTR before/after) · a falsifiability paragraph. **This is equally weighted with
everything else** — not a nice-to-have.

The raw material already exists, scattered, so this is assembly + writing, not research from
scratch:
- Four-identity / blast-radius tables — `design/01-high-level-design.md` (search "blast.radius"/
  "four.identity").
- Real measured numbers already on record in `CLAUDE.md`'s own changelog — e.g. `27ms → 1ms`
  (Session 26), `143.0ms → 155.0ms (failure)` (Session 43), recall similarity `~0.61-0.62` across
  multiple sessions, the RU/backup findings from this session.
- The AWS-services table and CockroachDB-tools statement are already drafted **verbatim** in
  `docs/submission-checklist.md §2` and `§5` — can be pasted into the README directly.

## The video shot list (~2:55, target under 3:00)

Built against Devpost's own stated guidance (shared by the user 2026-08-15): show-don't-tell,
live demo within the first 20-30s, name AWS/CockroachDB specifics on screen (not just narrated),
show memory being stored/retrieved/acted-on visibly, one-sentence problem+audience up front,
real-world user framing.

| Time | On screen | Audio / text overlay |
|---|---|---|
| 0:00–0:08 | Dashboard visible behind bold on-screen text | *Spoken + text:* "Engram is an autonomous DB reliability engineer for CockroachDB — built for teams who don't want to get paged at 3am for a missing index." |
| 0:08–0:15 | Cut straight to the live dashboard, URL bar visible | No narration — the URL bar itself proves "no login, guest-accessible." |
| 0:15–0:50 | **Incident #1**: Task Feed creates a task → Memory Inspector (empty) → Approval Queue shows the proposed `CREATE INDEX` → click Approve → Action Feed shows `success` + real latency number | Overlays timed to each panel: `"CockroachDB: VECTOR + C-SPANN — no memory yet, first time"` → `"AWS ECS Fargate — the agent, always running"` → the real measured number on screen |
| 0:50–1:15 | **Incident #2, same scope** — Memory Inspector shows a **similarity % badge** appear live next to confidence. Zoom in on this box specifically — the single strongest "memory in action" moment. | Overlay only: `"CockroachDB VECTOR index recalling a past fix — similarity 62%"`. Say almost nothing; let the number speak. |
| 1:15–1:25 | Quick cut to the real `BackupGateBlocked` log/screenshot captured 2026-08-15 | Overlay: `"CockroachDB Cloud REST API — backup-freshness gate. Refuses instead of risking an unrecoverable change."` |
| 1:25–2:05 | Terminal: `aws ecs stop-task` (via `scripts/demo_run.py kill-task`) → ECS console shows a replacement task → Action Feed / a quick SQL query proving **exactly one** remediation row | Overlays in sequence: `"AWS ECS Fargate — kill mid-fix"` → `"Amazon SQS FIFO — redelivers the same message"` → `"CockroachDB idempotency_key — exactly once, not twice"` |
| 2:05–2:25 | Fast montage/slide, 3-4 quick cuts, not narrated at length | On-screen list: `EventBridge (5-min sweep) · Lambda (memory lifecycle, separate from the agent) · CloudWatch (metrics) · S3 (artifacts) · No Bedrock — Ollama Cloud reasons, Cohere embeds` |
| 2:25–2:45 | Real-world framing, one line, still over the dashboard | *Spoken:* "This is what it looks like when your database fixes itself while you're asleep." |
| 2:45–2:55 | GitHub repo (License visible in About sidebar) + demo URL on screen | Close. |

**Never cut, whatever slips** (already a standing rule, `docs/submission-checklist.md` header):
kill-and-resume · the backup-gate refusal · the two-incident contrast · the licence · the
guest-accessible demo URL. All five are placed above — don't let editing pressure drop one.

## Production tips (from Devpost's own guidance, shared 2026-08-15)

- Write the narration out word-for-word and rehearse it against a stopwatch before recording —
  not yet done as of this writing.
- Test the mic for clear audio before a real take.
- Screen-record at a resolution where dashboard text/terminal output is actually legible —
  the dashboard has small badges/text (confidence %, similarity %) that need to read clearly.
- **Upload takes real time and must not happen day-of.** Target video export + upload by **end of
  Aug 17 evening at the latest** — leaves Aug 18 free for submission + a final playability check,
  not a race against upload time.
- Make sure the uploaded video is public or unlisted (never private) and playable with no login —
  verify this directly, don't assume.
- Submit on Devpost early enough to re-open the submission page yourself and confirm the video
  actually plays without login, per Devpost's own recommended check.

## Day-by-day timeline (today is 2026-08-15)

**Aug 15 (today, remainder)**
- Finish the in-progress rehearsal once the CockroachDB Cloud backup-gate freshness clears
  (target cluster backup was 47.6h old at last check, past the 24h window; a fresh nightly backup
  was expected around the 2026-08-15 00:00 UTC boundary — re-check `GET
  /api/v1/clusters/{id}/backups` before resuming, don't assume it cleared).
- Complete: incident #1 success (real `CREATE INDEX` applied + measured latency drop), incident
  #2's recall+success (the similarity-badge moment), and the kill-and-resume beat.
- **Screen-record this whole rehearsal at real speed** — this becomes the raw footage; don't treat
  "record the demo" as a separate future step from "finish the rehearsal."
- Keep today's real `BackupGateBlocked` moment (already captured) — reusable directly in the edit.

**Aug 16**
- Morning: write `README.md` (quickstart, diagram, tables, measured numbers, falsifiability
  paragraph — see "the gap found" section above for exactly what to pull from where). Paste in the
  AWS/CockroachDB written statements from `docs/submission-checklist.md §2/§5` verbatim.
- Afternoon: edit yesterday's raw footage down to under 3:00 against the shot list above, add
  the text overlays.
- Evening: check the cut against the five "never cut" items, one by one.

**Aug 17**
- Do **one more full dress rehearsal on fresh scenario tables** (Aug 15's tables have warm caches
  by now, and the backup-gate freshness will need re-checking again) — this is the safety margin,
  not optional. Re-record here if Aug 16's footage has gaps.
- Finalize video export and **upload by end of evening** (see production tips above — don't wait
  for Aug 18).
- Full QA pass: demo URL in an incognito window with zero prior login, README renders correctly
  on GitHub, License visible in the About sidebar.

**Aug 18 (submit by 12:00 ET)**
- Morning: one last stranger-eyes check of the demo URL and the uploaded video's playability.
- Submit well before noon, not at the wire.
- After submitting: **don't tear anything down** — keep ECS/CockroachDB/the dashboard alive
  through judging.

## Contingency: if the backup gate is still stale on recording day

Confirmed 2026-08-15: **there is no way to force an on-demand backup** for this cluster.
`POST /api/v1/clusters/{id}/backups` returns a hard `405 Method Not Allowed`, and the Cloud
console's own Backup Settings dialog states plainly: "Backup settings for Basic tier clusters
cannot be modified" — the schedule is a fixed "every 24 hours" with no manual trigger exposed
anywhere. The cluster's own backup history (`08-09, 08-11, 08-12, 08-13`, then gaps at `08-10`
and `08-14`) shows this schedule doesn't always fire reliably either — so freshness on any given
day is genuinely not something we control or can reliably predict.

**If Aug 17's dress rehearsal/recording hits the same `BackupGateBlocked` wall again**, don't burn
recording time forcing a "success" outcome that isn't available. Reframe instead:
- Lead Beat 1 with the **recall/similarity moment** (0:50–1:15 in the shot list) — this doesn't
  depend on the backup gate at all; `observe → recall → reason → gate` all complete and produce
  a real approval regardless of backup state. Only `act_measure`'s final DDL apply is gated.
- Frame the backup-gate block as the **intentional safety beat** for both incidents shown, not a
  workaround: "because a fresh backup isn't available right now, watch it correctly refuse to
  touch the database rather than risk an unrecoverable change" — this is honest, still
  interesting, and already on the "never cut" list regardless.
- For Beat 2 (kill-and-resume), the core proof — **exactly one decision/action row despite the
  kill** — still holds even if that one row's outcome is `blocked_by_backup_gate` rather than
  `success`. Less dazzling than a real applied fix, but still a complete, honest, technically
  valid demonstration of the resilience mechanism.
- Only if there's real schedule slack: delay recording by a day rather than force this fallback,
  if the extra day is actually available before the Aug 18 12:00 ET submission cutoff.

## Standing operational notes carried into this plan

- RU budget: target cluster capped at 45,000,000 as of 2026-08-15 (raised from 35M mid-session
  after hitting the cap during this rehearsal — see `CLAUDE.md §6` BLOCKING entry for the full
  history). Keep the user's friend's separate CockroachDB account as backup headroom specifically
  for Aug 17's dress rehearsal and the actual recording, not for casual re-testing.
- A real production bug was found and fixed this session: `consume_loop()` in `agent/main.py` had
  no exception handling and could die silently, invisible behind a healthy ECS health check —
  fixed, tested (226 unit tests pass), committed (`bed8fd4`), rebuilt, and redeployed live. Confirm
  this fix is still the live image before the Aug 17 dress rehearsal (check the running task's
  image digest via `python scripts/demo_run.py ecs-status`).
