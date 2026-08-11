"""Engram · agent/nodes/observe.py — fingerprint, anomaly rule, one-txn write.  [BRAINS]

design/02-low-level-design.md §5.1. Steps 2–4 only, stated up front:

  1. "Collect: MCP, probe SQL, CloudWatch, ccloud" — the "probe SQL" leg now
     exists (`agent/tools/sql_probe.py`, `SqlProbe.explain_analyze`); MCP,
     CloudWatch and ccloud still don't. `probe_result_from_explain()` below
     is the bridge from `SqlProbe`'s `ExplainResult` to this module's
     `ProbeResult` — real signal in, not a mock. The other three
     collection sources remain deliberately unimplemented: inventing their
     interfaces now, with nothing to call, would be speculative (coding-
     conduct rule 2). `ProbeResult` stays generic so any of them can feed
     it later without changing this module's other functions.
  2. Fingerprint: normalize the query text, sha256 it.
  3. Anomaly rule: deterministic, exactly the rule §5.1 names.
  4. One txn: `db.insert_incident_observation` (task + observation + entity,
     LLD's own "one txn" requirement — see that method's docstring for why
     it's a dedicated composite rather than three separate DAO calls), then
     the write-path embedding (Cohere `search_document`, D9 cache) into
     `memory_items(class='query_fingerprint')`, invariant #2.

Telemetry (step 5) and the MCP-timeout degrade path (step 6) are also out
of scope — no telemetry sink and no MCP adapter exist yet either.
"""

from __future__ import annotations

import hashlib
import re

from agent.memory.db import Database
from agent.memory.embeddings import embed_and_cache
from agent.providers.base import EmbeddingProvider
from agent.state import AgentState, Observation
from agent.tools.sql_probe import ExplainResult

DEFAULT_LATENCY_THRESHOLD_MS = 1000.0  # §5.1 step 3's "threshold" — tunable, not measured yet

# Collapses runs of digits and single/double-quoted strings to a placeholder —
# a defensible, stated simplification of "normalize SQL... collapse literals"
# (§5.1 step 2). A real normalizer (e.g. via a SQL parser) is out of scope.
_NUMBER_LITERAL = re.compile(r"\b\d+\b")
_STRING_LITERAL = re.compile(r"'[^']*'|\"[^\"]*\"")


class ProbeResult(dict):
    """The pre-collected signal this node processes — see the module
    docstring. Plain dict subclass (not a TypedDict) so callers can build
    one with a literal dict; documented here for the expected keys:

        query_text: str            raw SQL text for the slow query
        probe_latency_ms: float     EXPLAIN ANALYZE latency, milliseconds
        plan_has_seq_scan: bool     did the plan show a full/sequential scan
        index_candidate: str|None  column(s) the optimizer's recommendation named, if any
        table_name: str             the table the candidate index would live on
        target_cluster_id: str
    """


def probe_result_from_explain(
    result: ExplainResult, *, query_text: str, table_name: str, target_cluster_id: str
) -> ProbeResult:
    """Bridges `SqlProbe.explain_analyze()`'s real, measured output into
    this module's `ProbeResult` shape. The only place that conversion
    happens — kept here (not in `sql_probe.py`) so the dependency runs
    node -> tool, never the reverse; `agent/tools/sql_probe.py` knows
    nothing about `observe(node)` or `ProbeResult`.
    """
    return ProbeResult(
        query_text=query_text,
        probe_latency_ms=result.latency_ms,
        plan_has_seq_scan=result.has_full_scan,
        index_candidate=result.index_candidate,
        table_name=table_name,
        target_cluster_id=target_cluster_id,
    )


def normalize_query_text(sql: str) -> str:
    """§5.1 step 2: "normalize SQL (lowercase, collapse literals)." """
    text = sql.strip().lower()
    text = _STRING_LITERAL.sub("?", text)
    text = _NUMBER_LITERAL.sub("?", text)
    text = re.sub(r"\s+", " ", text)
    return text


def fingerprint(normalized_text: str) -> str:
    return hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()


def is_anomaly(probe: ProbeResult, *, latency_threshold_ms: float) -> bool:
    """§5.1 step 3, verbatim: "probe latency > threshold AND plan shows seq
    scan where index candidate exists." All three conditions, not any one.
    """
    return (
        probe["probe_latency_ms"] > latency_threshold_ms
        and probe["plan_has_seq_scan"]
        and bool(probe.get("index_candidate"))
    )


async def observe(
    state: AgentState,
    db: Database,
    embed_provider: EmbeddingProvider,
    probe: ProbeResult,
    *,
    scope_id: str,
    trigger: str = "manual",
    latency_threshold_ms: float = DEFAULT_LATENCY_THRESHOLD_MS,
) -> dict:
    """Returns a partial `AgentState` update: `task_id`, `scope_id`,
    `target_cluster_id`, `trigger`, `phase`, `observations` (appended),
    `incident_fingerprint`.

    `state` is read for its existing `observations` (appended to, not
    replaced — a sweep can accumulate several probes before an incident
    task exists) and otherwise only provides continuity across calls; a
    fresh call with `state["observations"] == []` is a normal first probe.
    """
    normalized = normalize_query_text(probe["query_text"])
    fp = fingerprint(normalized)
    incident = is_anomaly(probe, latency_threshold_ms=latency_threshold_ms)
    task_type = "incident" if incident else "sweep"

    task_id, observation_id, entity_id = await db.insert_incident_observation(
        scope_id,
        task_type,
        trigger,
        target_cluster_id=probe["target_cluster_id"],
        incident_fingerprint=fp if incident else None,
        source="sql_probe",
        kind="query_stats",
        payload={
            "text": normalized,
            "raw_text": probe["query_text"],  # runnable SQL -- normalized "text" has literals
                                               # stripped to '?' for fingerprinting/embedding and
                                               # is NOT valid SQL; act_measure(node) needs THIS for
                                               # its own before/after EXPLAIN ANALYZE measurement
            "latency_ms": probe["probe_latency_ms"],
            "plan_has_seq_scan": probe["plan_has_seq_scan"],
            "index_candidate": probe.get("index_candidate"),
        },
        entity_kind="table",
        entity_name=probe["table_name"],
    )

    # Write-path embedding (LLD §5.1 step 4, invariant #2): search_document,
    # cache-aware (D9) — a repeat fingerprint across sweeps never re-embeds.
    vectors = await embed_and_cache(db, embed_provider, [normalized], "search_document")
    await db.insert_memory_item(
        scope_id,
        "query_fingerprint",
        normalized,
        embedding=vectors[0],
        entity_id=entity_id,
        provenance={"task_id": task_id, "observation_id": observation_id},
    )

    observation: Observation = {
        "source": "sql_probe",
        "kind": "query_stats",
        "fingerprint": fp,
        "entity_ids": [entity_id],
        "payload": {
            "text": normalized,
            "raw_text": probe["query_text"],  # see the identical field above -- act_measure needs this
            "latency_ms": probe["probe_latency_ms"],
            "plan_has_seq_scan": probe["plan_has_seq_scan"],
            "index_candidate": probe.get("index_candidate"),
        },
    }

    return {
        "task_id": task_id,
        "scope_id": scope_id,
        "target_cluster_id": probe["target_cluster_id"],
        "trigger": trigger,
        "phase": "observe",
        "observations": [*state.get("observations", []), observation],
        "incident_fingerprint": fp if incident else None,
    }
