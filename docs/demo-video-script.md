# Demo video — recording plan + voiceover script

> Written 2026-08-16. Companion to `docs/demo-video-plan.md` (day-by-day timeline, contingency
> plan, real measured numbers) and `docs/friend-account-setup.md` (the cluster switch this
> recording depends on). This file is the operational one: exactly what to have open, what to do,
> in what order, and the word-for-word narration to record separately afterward.

## Recording approach (decided 2026-08-16)

Record **one continuous take**, straight through, silently (no live narration — voiceover is
recorded separately afterward and laid over the edited cut). Don't try to hit exact target
durations live — the edit trims/cuts/speeds up afterward. Leave ~2s of stillness before/after
every tab switch so there's a clean cut point.

**Precondition: do not start the real take until the friend cluster's first backup has landed**
(check via the same script used throughout this session — see "Before you start recording"
below). Recording now would only produce `parked`/backup-gate-refused outcomes for incidents
that are supposed to succeed.

## Tab/window inventory — prepare ALL of these before hitting record

One browser window, 3 tabs (switch with Ctrl+Tab — faster and cleaner to record than Alt-Tab
between separate windows):

1. **Tab 1 — Dashboard**: `https://dashboard-five-chi-90.vercel.app`
2. **Tab 2 — AWS Console, ECS**: pre-navigate to the `engram-agent-cluster` → `engram-agent`
   service page (logged in ahead of time, sitting on this exact page, not the console homepage).
3. **Tab 3 — GitHub**: the repo's front page (README should be pushed and rendering by the time
   you record this — see the git/README work done alongside this file).

Plus one separate app:

4. **Terminal** (PowerShell or Git Bash), already `cd`'d into the repo, font size large enough
   to read on a recording, commands ready to paste (not hand-typed live, to avoid typos on
   camera).

Before recording: close/mute anything that could pop a notification on screen (email, Slack,
Windows notifications), and confirm the browser is in a normal windowed view (not fullscreen
kiosk mode) so the **URL bar stays visible** — that's the "no login required" proof.

## Table assignment (which of the 4 remaining fresh tables maps to which beat)

`demo_final_remembers_1` is already burned (used in the Step 10 test — its cache is warm, don't
reuse it for a "cold, slow" measurement). Of the 4 fresh tables left:

| Table | Beat | Why |
|---|---|---|
| `demo_final_remembers_2` | Incident #1 (first fix) | Fresh, never touched |
| `demo_final_fallback_1` | Incident #2 (recall/similarity beat) | Fresh — doesn't need to match incident #1's table, recall matches by query-shape similarity, not literal table identity (confirmed in Step 10: a brand-new table still got a 0.616-similarity citation) |
| `demo_final_survives` | Beat 2 (kill-and-resume) | Fresh, needs to be genuinely uninterrupted going in |
| `demo_final_fallback_2` | Spare | Untouched, only use if something above needs a redo |

## Before you start recording

```
python -c "
import os, json
from dotenv import load_dotenv
load_dotenv()
import httpx
token = os.environ['CCLOUD_TOKEN']
cluster_id = os.environ['ENGRAM_TARGET_CLUSTER_ID']
r = httpx.get(f'https://cockroachlabs.cloud/api/v1/clusters/{cluster_id}/backups', headers={'Authorization': f'Bearer {token}'}, timeout=20)
print(r.status_code); print(json.dumps(r.json(), indent=2))
"
```
Confirm `backups` is non-empty before doing the real take.

## Live run-sheet (record in this order, one continuous take)

| Step | Screen | Action |
|---|---|---|
| 1 | Tab 1 (Dashboard) | Sit idle 5-10s, URL bar visible |
| 2 | Terminal | Run `python scripts/demo_run.py send --table demo_final_remembers_2 --scope demo-final-1` |
| 3 | Tab 1 (Dashboard) | Watch Task Feed → Approval Queue shows pending → click Approve → Action Feed reaches `success` + real latency |
| 4 | Terminal | Run `python scripts/demo_run.py send --table demo_final_fallback_1 --scope demo-final-2` |
| 5 | Tab 1 (Dashboard) | Zoom into Memory Inspector — wait for the similarity badge to appear |
| 6 | *(no live tab)* | Skip — the backup-gate-refusal beat is a still image inserted in the edit (this session's own `demo-final` parked task, already captured) |
| 7 | Terminal | Run `python scripts/demo_run.py send --table demo_final_survives --scope demo-final-3` |
| 8 | Tab 1 (Dashboard) | Wait until its approval shows **pending** — do NOT approve yet |
| 9 | Terminal | Run `python scripts/demo_run.py kill-task` |
| 10 | Tab 2 (AWS Console/ECS) | Wait for the replacement task to reach `RUNNING` |
| 11 | Tab 1 (Dashboard) | Click Approve on the still-pending approval now |
| 12 | Terminal | Run a quick count query confirming exactly one `remediation_actions` row for that task |
| 13 | Tab 1 (Dashboard) | Show the task reach `completed`, Action Feed `success` |
| 14 | Tab 1 (Dashboard) | Sit idle for the closing beat |
| 15 | Tab 3 (GitHub) | Show README + License badge in the About sidebar |

**Recommended: do a dry run of the tab-switching mechanics only (no real `send` commands) first**,
to build muscle memory for the sequence, before spending any of the 4 remaining fresh tables on
the real take.

## Recording tool + settings (Windows 11)

- **Tool**: Xbox Game Bar (`Win+G`, built in, zero install) for simplicity, or OBS Studio if you
  want more control over scene/source switching. Either is fine for tab-switching capture.
- **Resolution**: 1920×1080 or higher — the dashboard's small badges (similarity %, confidence)
  need to stay legible.
- **Frame rate**: 30fps is enough.
- **Audio**: mute/disable mic input for this recording — voiceover is added separately in post.

## After recording

1. Cut the continuous take into the segments below.
2. Speed up any real-time waits (reason/gate round-trips, ECS replacement-task startup) 3-4x,
   labeled "sped up" on screen if it's a long wait.
3. Insert the backup-gate-refusal still image where step 6 was skipped.
4. Add text overlays per the table below.
5. Record the voiceover separately (quiet room, no live system audio) and lay it over the cut,
   matching timestamps loosely — the target durations already have slack built in.
6. Export, upload (per `docs/demo-video-plan.md`'s production tips — don't leave this for the
   last day), and confirm it plays without login in an incognito window before submitting.

---

## Voiceover script (word-for-word, ~386 words, ~2:34 spoken inside a 2:55 edited runtime)

| Time | On screen | Voiceover |
|---|---|---|
| 0:00–0:08 | Dashboard, idle, bold text overlay | *"Engram is an autonomous database reliability engineer for CockroachDB — for teams who don't want to get paged at 3 AM for a missing index."* |
| 0:08–0:15 | Dashboard, URL bar visible | *"This is the live dashboard — no login, no credentials. Anyone can watch it work."* |
| 0:15–0:50 | Incident #1 live | *"Watch it catch a real problem on its own — a query scanning two million rows on every call. It reasons about the fix using CockroachDB's own query plan, then stops and asks for one human approval before touching anything. Approved — and it applies a real CREATE INDEX, live, on the actual cluster. Eleven hundred milliseconds down to nineteen — confirmed by CockroachDB itself, not just claimed."* |
| 0:50–1:15 | Incident #2, Memory Inspector zoom | *"Now a second, different incident. Look at the Memory Inspector — sixty-two percent similarity. Engram recognized this shape of problem from what it already fixed, and cited its own past decision before proposing anything new. This is CockroachDB's VECTOR type and C-SPANN index doing real semantic recall, not a lookup table."* |
| 1:15–1:25 | Backup-gate still image | *"Not every fix should happen automatically. Here, Engram calls CockroachDB Cloud's own backup API first — and when no safe backup exists yet, it refuses to touch the database at all."* |
| 1:25–2:05 | Kill-task → ECS console → terminal → dashboard | *"Now the resilience test. While Engram is mid-fix, we kill its AWS ECS Fargate task outright — no warning. A replacement task starts automatically within seconds. And back on CockroachDB, the ledger proves it: exactly one remediation row for the whole episode — not two, not a duplicate, one. The new task picked up the exact same message, resumed from a real checkpoint, and finished the job the old one couldn't."* |
| 2:05–2:25 | AWS-services slide | *"None of this uses Amazon Bedrock — the reasoning is Ollama Cloud, the embeddings are Cohere. AWS's job here is durability: ECS Fargate runs the agent, SQS and EventBridge trigger it, Lambda handles memory maintenance separately so it survives even if the agent doesn't, and CloudWatch and S3 keep the record."* |
| 2:25–2:45 | Dashboard, idle | *"This is what it looks like when your database fixes itself while you're asleep. Engram is built for the on-call engineer who shouldn't be the last line of defense against a bad query plan — and it remembers every decision it makes, with the evidence to prove it."* |
| 2:45–2:55 | GitHub + demo URL | *"Public repo, Apache 2.0 licensed, live demo URL on screen now — go try it yourself."* |

Covers every "never cut" item from `CLAUDE.md`/`docs/submission-checklist.md` (kill-and-resume ·
backup-gate refusal · two-incident contrast · license · guest-accessible demo URL) and states
"no Bedrock" plainly, out loud, per §2's own written-statement requirement.
