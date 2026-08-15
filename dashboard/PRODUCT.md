# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary: hackathon judges evaluating the Engram submission, watching the live judged
demo run on this screen. No secondary audience is recorded — future work should not assume
day-to-day operator use unless that's confirmed later.

## Product Purpose

The read-only observability surface (plus one mutating action) over the Engram autonomous
CockroachDB reliability agent. It exists so a judge can watch the agent's two demo beats
happen live, in real time, without reading logs or querying the database directly: "it
remembers" (a recall hit against a prior incident, with similarity/confidence/provenance
visible) and "it survives" (an `aws ecs stop-task` kill mid-remediation, a new task resuming
from checkpoint). Success is a judge watching an incident get observed, recalled, reasoned
about, gated for human approval, and remediated — entirely from this screen.

## Positioning

This is a live view directly over the same CockroachDB "memory" cluster the agent itself
reads and writes (via a dedicated read-only role, `engram_reader`), not a mocked demo UI or
a polled copy of the data. The one mutating action (Approve/Reject) round-trips through a
real, separately deployed API Gateway + Lambda. What's on screen is the literal state of a
running system, not a staged one.

## Operating Context

- Viewed during a live, judged demo window (hackathon submission, deadline 2026-08-18
  17:00 ET).
- Backed by real, live AWS infrastructure (ECS Fargate agent, SQS, three lifecycle Lambdas,
  API Gateway) and a real CockroachDB Cloud cluster — data on screen reflects real incidents
  from real live test runs, not fixtures.
- No CockroachDB changefeeds (RU-budget constraint, free-tier limit); every feed is a
  server-side cursor poll over SSE, not client-side polling and not a live subscription.
- The underlying CockroachDB cluster has a tight, actively-monitored RU budget — this
  dashboard's own read cost must stay negligible against that budget.

## Capabilities and Constraints

- Four read-only SSE feeds (recent tasks, action feed, memory inspector, approval queue)
  plus one metrics panel backed by CloudWatch, not the database.
- Exactly one mutating action anywhere in this app: Approve/Reject on a pending approval,
  proxied through a server-side route to API Gateway/Lambda. No database write credential,
  and no database credential of any kind, ever reaches the browser — no `NEXT_PUBLIC_`
  environment variable exists in this codebase.
- Single-screen layout is a hard constraint: the console must fit 100vh with no page-level
  scroll. Individual feed panels may scroll their own content internally.
- Dark mode only, with no light-mode toggle — this is an internal ops console with one
  fixed viewing context, not a public product with a user preference to respect.

## Brand Commitments

Name: "Engram". No tagline, logo, or tone-of-voice commitment exists yet.

The user volunteered a binding visual constraint for this surface, recorded here as fact
only (per init's own rule: not expanded into design-system decisions during this step) —
brutalist, high-density DevOps console; strict dark mode on a zinc-950 ground; zero
glassmorphism, drop shadows, glowing edges, or gradients; flat, sharp 1px borders; strict
monospace for all telemetry, metrics, ID hashes, and SQL/log text. The design-system
decisions this implies belong in `DESIGN.md` (via `document` or `new-work`), not here.

## Evidence on Hand

- Real deployed AWS infrastructure: ECS Fargate agent, SQS/EventBridge, three lifecycle
  Lambdas, API Gateway + approvals Lambda — all live as of 2026-08-14.
- Real historical demo data already sitting in the memory cluster from prior live test
  runs (e.g. `incident-test-bigger`, `kill-test-a5fb74a5`), visible today in the Memory
  Inspector and Action Feed panels.
- No user testimonials, pricing, or case studies exist, and none should be fabricated —
  this is a hackathon submission, not a monetized product.

## Product Principles

1. Show the real system, never a staged one — every panel reflects live DB/AWS state.
2. The demo beats are the product. Recall hit with confidence/provenance, and
   kill-and-resume, must be legible on this screen without narration.
3. No database credential in the browser, ever. Every mutation proxies through a
   server-side route.
4. Judge-first legibility. A judge should never need to read raw SQL or logs to understand
   what happened, though raw SQL stays available (collapsed) for verification.
5. Cheap by construction. Server-side cursor-polling SSE, never changefeeds; this
   dashboard's own read cost stays negligible against a tight RU budget.

## Accessibility & Inclusion

No specific accessibility standard was established as a product requirement (confirmed).
