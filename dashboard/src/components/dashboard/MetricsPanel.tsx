"use client";

import { useEffect, useRef, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ChartSeries, LineChart } from "@/components/dashboard/LineChart";
import { StatTile } from "@/components/dashboard/StatTile";
import type { MetricsResponse } from "@/lib/types";

// design/02-low-level-design.md §12's dashboard metric table, fetched via the real deployed
// GET /metrics Lambda (workers/metrics/handler.py, CloudWatch GetMetricData) -- NOT the memory
// cluster, no engram_reader involvement, zero CockroachDB RU cost for this whole panel.
// Polls every 30s to match that Lambda's own server-side cache TTL -- polling faster would just
// re-fetch the identical cached body.

const POLL_INTERVAL_MS = 30_000;
const WINDOWS = ["1h", "6h", "24h", "7d"] as const;
type Window = (typeof WINDOWS)[number];

const CHART_COLORS = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
];

function dimensionLabel(dims: Record<string, string>): string {
  const entries = Object.entries(dims);
  if (entries.length === 0) return "overall";
  if (entries.length === 1) return entries[0][1];
  return entries.map(([k, v]) => `${k}=${v}`).join(", ");
}

function toChartSeries(raw: MetricsResponse["metrics"][string] | undefined): ChartSeries[] {
  if (!raw) return [];
  // Stable sort by dimension identity so a series keeps the SAME color across re-fetches/polls
  // (color follows the entity, never its position in an arbitrary API response order).
  const sorted = [...raw].sort((a, b) =>
    JSON.stringify(a.dimensions).localeCompare(JSON.stringify(b.dimensions))
  );
  return sorted.map((s, i) => ({
    key: JSON.stringify(s.dimensions),
    label: dimensionLabel(s.dimensions),
    color: CHART_COLORS[i % CHART_COLORS.length],
    datapoints: s.datapoints,
  }));
}

function latestValue(raw: MetricsResponse["metrics"][string] | undefined): number | null {
  if (!raw || raw.length === 0) return null;
  let best: { t: number; v: number } | null = null;
  for (const series of raw) {
    for (const dp of series.datapoints) {
      const t = new Date(dp.timestamp).getTime();
      if (!best || t > best.t) best = { t, v: dp.value };
    }
  }
  return best?.v ?? null;
}

function sumValue(raw: MetricsResponse["metrics"][string] | undefined): number | null {
  if (!raw || raw.length === 0) return null;
  let total = 0;
  let any = false;
  for (const series of raw) {
    for (const dp of series.datapoints) {
      total += dp.value;
      any = true;
    }
  }
  return any ? total : null;
}

function formatMs(v: number | null): string {
  if (v === null) return "—";
  return v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${v.toFixed(0)} ms`;
}

function formatSeconds(v: number | null): string {
  if (v === null) return "—";
  return `${v.toFixed(1)} s`;
}

function formatCount(v: number | null): string {
  if (v === null) return "—";
  return String(Math.round(v));
}

export function MetricsPanel() {
  const [window, setWindow] = useState<Window>("6h");
  const [data, setData] = useState<MetricsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef(false);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      if (inFlight.current) return; // refetch keeps the frame -- never stack overlapping polls
      inFlight.current = true;
      try {
        const res = await fetch(`/api/metrics?window=${window}`, { cache: "no-store" });
        const body = await res.json();
        if (cancelled) return;
        if (!res.ok) {
          setError(body.error ?? `HTTP ${res.status}`);
          return;
        }
        setError(null);
        setData(body as MetricsResponse);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      } finally {
        inFlight.current = false;
      }
    }

    load();
    const interval = setInterval(load, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [window]);

  const metrics = data?.metrics;

  return (
    <Card className="md:col-span-2 xl:col-span-4">
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center justify-between gap-2">
          <span>Metrics</span>
          <div className="flex items-center gap-2">
            {/* Filters: one row, above the charts, date-range presets (references/interaction.md) */}
            <div className="flex gap-1 rounded-md border p-0.5">
              {WINDOWS.map((w) => (
                <button
                  key={w}
                  onClick={() => setWindow(w)}
                  className={`rounded px-2 py-1 text-xs font-medium transition-colors ${
                    w === window
                      ? "bg-primary text-primary-foreground"
                      : "text-muted-foreground hover:bg-accent"
                  }`}
                >
                  {w}
                </button>
              ))}
            </div>
            {data && (
              <span className="text-xs text-muted-foreground">
                {data.cached ? "cached" : "live"} · {new Date(data.generated_at).toLocaleTimeString()}
              </span>
            )}
          </div>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        {error && (
          <p className="rounded-md border border-destructive/30 bg-destructive/10 p-2 text-sm text-destructive">
            {error}
          </p>
        )}

        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          <div>
            <h3 className="mb-2 text-sm font-medium">Recall hit rate</h3>
            <LineChart
              series={toChartSeries(metrics?.["recall_hit_rate"])}
              yDomain={[0, 1]}
              yFormat={(v) => `${Math.round(v * 100)}%`}
              emptyLabel="No recall events yet in this window."
            />
          </div>
          <div>
            <h3 className="mb-2 text-sm font-medium">LLM token usage</h3>
            <LineChart
              series={toChartSeries(metrics?.["llm_token_usage"])}
              emptyLabel="No LLM calls yet in this window."
            />
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          <StatTile label="Time to remediation" value={formatSeconds(latestValue(metrics?.["time_to_remediation"]))} />
          <StatTile label="LLM latency" value={formatMs(latestValue(metrics?.["llm_latency_ms"]))} />
          <StatTile label="Sweep cycle" value={formatMs(latestValue(metrics?.["sweep_cycle_ms"]))} />
          <StatTile label="Blocked by backup gate" value={formatCount(sumValue(metrics?.["blocked_by_backup_gate"]))} hint="this window" />
          <StatTile label="Exactly-once conflicts" value={formatCount(sumValue(metrics?.["exactly_once_conflicts_detected"]))} hint="this window" />
          <StatTile label="LLM failures" value={formatCount(sumValue(metrics?.["llm_failures"]))} hint="this window" />
        </div>

        {data && data.omitted.length > 0 && (
          <p className="text-xs text-muted-foreground">
            Not tracked yet: {data.omitted.join("; ")}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
