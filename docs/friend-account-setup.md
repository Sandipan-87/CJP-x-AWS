# Setting up CockroachDB in a friend's account — step by step

> Written 2026-08-15, for switching the deployed agent's TARGET cluster to a genuinely separate
> CockroachDB Cloud organization (a friend's new account) ahead of the final demo recording, to
> get a fresh 50M RU/month allowance and a clean backup-freshness clock. This does NOT touch the
> MEMORY cluster (`ENGRAM_MEMORY_DSN`) — that stays exactly as-is; only the TARGET cluster (the
> subject being operated on, CLAUDE.md §2's "two clusters, two roles") changes.

**Budget real time for this — it's not a five-minute swap.** Plan for it to take a couple of
hours across steps 1-8, done well before the actual recording session, plus a real ~24h wait
after cluster creation before its first automatic backup exists (same behavior we hit today —
see `docs/demo-video-plan.md`'s contingency section).

---

## Step 1 — Friend creates the CockroachDB Cloud account + cluster

1. Friend signs up at [cockroachlabs.cloud](https://cockroachlabs.cloud) (free, no credit card
   required — confirmed via real pricing lookup this session: Basic tier is genuinely $0 until
   50M RU / 10 GiB per month, org-wide).
2. Create a new cluster: **Basic tier** (formerly "Serverless"), any region (prefer `us-east-1`
   to match the agent's own ECS region — same-region keeps latency/timing behavior consistent
   with what we already tuned today, per the network-distance findings in `CLAUDE.md`'s own
   history).
3. Name it something identifiable, e.g. `engram-target-friend`.

## Step 2 — Get the connection details

From the CockroachDB Cloud console, on the new cluster's page:

1. **Cluster ID** — visible in the URL (`.../clusters/<uuid>/...`) or the cluster's overview page.
2. **Admin connection string** — Connect panel → SQL user `admin` (or whatever the default is) →
   copy the full `postgresql://...` connection string. This becomes the new `ENGRAM_TARGET_DSN`.
3. **CA certificate** — same Connect panel has a "Download CA Cert" link. **Do this even though
   this project's other cluster shares one CA** — a *different organization's* cluster may not
   share the same root CA. Save it locally, e.g. `friend-target-ca.crt`.

## Step 3 — Get a `CCLOUD_TOKEN` scoped to the new cluster

Friend needs to create a Service Account API key (Cluster Admin scope) via the console:
**Access → Service Accounts → API Keys** (same path this project's own `.env.example` documents
for the existing token). This key must be scoped to the NEW cluster specifically — the existing
`CCLOUD_TOKEN` in `.env` is scoped to the current target cluster and will `403` against the new
one (confirmed behavior: `CCLOUD_TOKEN` is Cluster-Admin-scoped per-cluster, not org-wide, per
Session 29's own finding).

## Step 4 — Update local `.env`

Change these three values (everything else — `ENGRAM_MEMORY_DSN`, `COHERE_API_KEY`,
`OLLAMA_API_KEY` — stays untouched):

```
ENGRAM_TARGET_DSN=<the new admin connection string from Step 2>
ENGRAM_TARGET_CLUSTER_ID=<the new cluster UUID from Step 2>
CCLOUD_TOKEN=<the new Service Account API key from Step 3>
```

Keep a copy of the OLD values somewhere before overwriting — you'll want them back if you ever
need the current sandbox cluster again.

## Step 5 — Bootstrap `engram_probe`/`engram_operator` on the new cluster

This is the exact script that already did this for the current cluster (`scripts/
bootstrap_target_roles.py`) — it reads `ENGRAM_TARGET_DSN` from `.env` (now pointing at the new
cluster), runs `db/target/001_target_roles.sql`, sets random passwords, live-verifies the
privilege boundary, and writes `ENGRAM_TARGET_PROBE_DSN`/`ENGRAM_TARGET_OPERATOR_DSN` into `.env`
automatically:

```
python scripts/bootstrap_target_roles.py --sslrootcert friend-target-ca.crt
```

Expect the same 7/7-style live-verified output the original run had — `engram_probe` can SELECT
but not CREATE INDEX, `engram_operator` can CREATE INDEX/ANALYZE but not DROP/GRANT. If anything
here fails, stop and fix it before proceeding — the agent depends on both roles existing
correctly.

## Step 6 — Verify the new `CCLOUD_TOKEN` is correctly scoped

```
python scripts/verify_ccloud.py
```

This is the same defensive gate script that caught the wrong-scope mistake in Session 29 — it
should now show a real `200` against the new target cluster (not the old one). If it 403s,
double-check the Service Account key was actually scoped to the new cluster's UUID, not the old
one.

## Step 7 — Update the deployed agent's AWS Secrets Manager entry

The agent reads `ENGRAM_TARGET_DSN`, `ENGRAM_TARGET_PROBE_DSN`, `ENGRAM_TARGET_OPERATOR_DSN`, and
`CCLOUD_TOKEN` from one JSON secret, `engram/agent-secrets` (confirmed directly in
`infra/engram_infra/agent_stack.py`'s `SECRET_ENV_KEYS`). With `.env` now updated (Steps 4-5),
re-run the same script that created this secret originally — it correctly detects the secret
already exists and updates it in place rather than failing:

```
# temporarily export engram-deploy's credentials (never write them to .env)
export AWS_ACCESS_KEY_ID=<engram-deploy access key>
export AWS_SECRET_ACCESS_KEY=<engram-deploy secret key>
python scripts/bootstrap_agent_infra.py
```

Look for: `Secrets Manager secret already existed -- value updated`.

**Note:** `ENGRAM_TARGET_CLUSTER_ID` is NOT part of this secret — it's not a container env var at
all. It only matters locally, because `scripts/demo_run.py send` reads it from your own `.env`
to build the SQS message. Step 4 already covers this.

## Step 8 — Force the ECS agent to pick up the new secret

Secrets Manager values are only read at container startup, so a running task won't see the
update on its own:

```python
import boto3, os
from dotenv import load_dotenv
load_dotenv()
s = boto3.Session(aws_access_key_id=os.environ['AWS_DEPLOY_ACCESS_KEY_ID'], aws_secret_access_key=os.environ['AWS_DEPLOY_SECRET_ACCESS_KEY'], region_name='us-east-1')
s.client('ecs').update_service(cluster='engram-agent-cluster', service='engram-agent', forceNewDeployment=True)
```

Then poll `python scripts/demo_run.py ecs-status` until a new task ARN is `RUNNING`/`HEALTHY`,
and pull its logs (same pattern used throughout today's session) to confirm a clean startup —
`DB reachable` (this checks the MEMORY cluster, unaffected), `Cohere reachable`, `Ollama
reachable`, `lease round-trip OK`. None of these checks directly touch the new TARGET cluster —
that only gets exercised the first time a real message is sent (Step 10).

## Step 9 — Build scenario tables

**Use ~2-2.5M rows, not 1.5M** — today's real finding was that 1.5M rows sits right at an
ambiguous boundary where CockroachDB's own `EXPLAIN ANALYZE` output rounds away sub-second
precision (see `docs/demo-video-plan.md`'s rounding-precision section). Build fresh, distinctly
named tables for each planned beat:

```
python scripts/demo_run.py build --table demo_final_remembers_1 --rows 2000000
python scripts/demo_run.py build --table demo_final_remembers_2 --rows 2000000
python scripts/demo_run.py build --table demo_final_survives    --rows 2000000
```

## Step 10 — Verify end to end, then wait for the backup

Send one real incident and confirm it classifies correctly, recall/reason/gate all fire, and — if
a backup already exists by then — that `act_measure` actually applies:

```
python scripts/demo_run.py send --table demo_final_remembers_1 --scope demo-final
python scripts/demo_run.py watch --scope demo-final
```

**Expect the backup gate to block this first attempt** — a brand-new cluster starts with zero
backups. Check the backups API (same pattern used all session) before assuming otherwise:

```python
import os, json, httpx
from datetime import datetime, timezone
token = os.environ['CCLOUD_TOKEN']  # now the new one
cluster_id = os.environ['ENGRAM_TARGET_CLUSTER_ID']  # now the new one
r = httpx.get(f'https://cockroachlabs.cloud/api/v1/clusters/{cluster_id}/backups', headers={'Authorization': f'Bearer {token}'}, timeout=20)
print(r.json())
```

An empty `backups` list is expected and normal at first — this is the same "fresh Basic cluster,
empty list, gate defaults to refuse" case `docs/external-constraints.md` already documents as
the intended demo beat, not a bug. Once the first backup lands (watch for it, same pattern as
today — don't assume a fixed time), re-run Step 10's send against a **fresh** table (build a new
one, since the first attempt's table cache is now warm) for the actual clean recording take.

---

## Quick-reference: what changes vs. what stays the same

| Stays the same | Changes |
|---|---|
| `ENGRAM_MEMORY_DSN` (memory cluster) | `ENGRAM_TARGET_DSN` |
| `COHERE_API_KEY` | `ENGRAM_TARGET_CLUSTER_ID` |
| `OLLAMA_API_KEY` | `ENGRAM_TARGET_PROBE_DSN` / `ENGRAM_TARGET_OPERATOR_DSN` (regenerated by Step 5) |
| ECS cluster/service/task definition themselves | `CCLOUD_TOKEN` |
| The dashboard (`ENGRAM_READER_DSN` points at the memory cluster, unaffected) | The one `engram/agent-secrets` Secrets Manager value (Step 7) |
