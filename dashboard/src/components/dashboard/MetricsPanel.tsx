"use client";

import { useEffect, useRef, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
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
    // h-full, not a spanning grid cell -- this panel is now the whole third column of the
    // page's 3-column cockpit (page.tsx), not a wide row underneath a 4-column grid.
    <Card className="flex h-full flex-col">
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center justify-between gap-3 text-xs tracking-widest text-muted-foreground uppercase">
          <span>Metrics</span>
          <div className="flex items-center gap-3">
            {/* Filters: one row, above the charts, date-range presets (references/interaction.md) */}
            <div className="flex gap-1 border border-border p-0.5">
              {WINDOWS.map((w) => (
                <button
                  key={w}
                  onClick={() => setWindow(w)}
                  className={`px-2.5 py-1 font-mono text-xs font-medium normal-case transition-colors ${
                    w === window
                      ? "bg-foreground text-background"
                      : "text-muted-foreground hover:bg-accent"
                  }`}
                >
                  {w}
                </button>
              ))}
            </div>
            {data && (
              <span className="font-mono normal-case">
                {data.cached ? "cached" : "live"} · {new Date(data.generated_at).toLocaleTimeString()}
              </span>
            )}
          </div>
        </CardTitle>
      </CardHeader>
      {/* Internal scroll, same convention as every other feed panel -- this column never
          forces the page itself to scroll (PRODUCT.md/DESIGN.md's fixed 100vh cockpit). */}
      <CardContent className="flex-1 min-h-0 overflow-hidden">
        <ScrollArea className="h-full">
          <div className="flex flex-col gap-4 pr-2">
            {error && (
              <p className="border border-red-900 bg-red-950 p-3 text-sm text-red-400">
                {error}
              </p>
            )}

            {/* Stacked, not side-by-side -- this column is roughly a third of the old
                full-width row, too narrow for two charts abreast. */}
            <div className="flex flex-col gap-4">
              <div>
                <h3 className="mb-2 text-xs tracking-wide text-muted-foreground uppercase">Recall hit rate</h3>
                <LineChart
                  height={140}
                  series={toChartSeries(metrics?.["recall_hit_rate"])}
                  yDomain={[0, 1]}
                  yFormat={(v) => `${Math.round(v * 100)}%`}
                  emptyLabel="No recall events yet in this window."
                />
              </div>
              <div>
                <h3 className="mb-2 text-xs tracking-wide text-muted-foreground uppercase">LLM token usage</h3>
                <LineChart
                  height={140}
                  series={toChartSeries(metrics?.["llm_token_usage"])}
                  emptyLabel="No LLM calls yet in this window."
                />
              </div>
            </div>

            {/* One outer border, hairline dividers between cells -- a flat stat strip, not
                six separately-boxed cards. See StatTile's own comment. 3 columns (2 rows),
                not 6-across -- this is a third of the old full-width row's space. */}
            <div className="grid grid-cols-3 divide-x divide-y divide-border border border-border">
              <StatTile label="Time to remediation" value={formatSeconds(latestValue(metrics?.["time_to_remediation"]))} />
              <StatTile label="LLM latency" value={formatMs(latestValue(metrics?.["llm_latency_ms"]))} />
              <StatTile label="Sweep cycle" value={formatMs(latestValue(metrics?.["sweep_cycle_ms"]))} />
              <StatTile label="Blocked by backup gate" value={formatCount(sumValue(metrics?.["blocked_by_backup_gate"]))} hint="this window" />
              <StatTile label="Exactly-once conflicts" value={formatCount(sumValue(metrics?.["exactly_once_conflicts_detected"]))} hint="this window" />
              <StatTile label="LLM failures" value={formatCount(sumValue(metrics?.["llm_failures"]))} hint="this window" />
            </div>
          </div>
        </ScrollArea>
      </CardContent>
    </Card>
  );
}
