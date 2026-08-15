"use client";

import { useMemo, useRef, useState } from "react";
import type { MetricSeries } from "@/lib/types";

// Hand-rolled SVG line chart -- no charting library is a dependency anywhere in this repo, and
// this dashboard's package.json deliberately stays minimal (see its own README). Mark specs and
// interaction rules follow the dataviz skill (references/marks-and-anatomy.md,
// references/interaction.md): 2px lines, >=8px end markers with a surface-color ring, hairline
// gridlines, a crosshair+tooltip that lists every series at the hovered X, and a legend whenever
// there's more than one series (never make the reader color-match alone).
//
// Deliberately flat: no area-fill gradient, no glow filter, no shadow anywhere in this file --
// a flat, dense, minimalist dev-tool look means the 2px line and the end marker ARE the whole
// mark, nothing softened or backlit behind them. Numeric/timestamp text uses --font-mono.

export interface ChartSeries {
  key: string;
  label: string;
  color: string; // CSS color (a --chart-N token value)
  datapoints: MetricSeries["datapoints"] | { timestamp: string; value: number }[];
}

interface LineChartProps {
  series: ChartSeries[];
  height?: number;
  yDomain?: [number, number];
  yFormat?: (v: number) => string;
  emptyLabel?: string;
}

const BASE_MARGIN = { top: 12, right: 12, bottom: 24, left: 44 };
const CHART_WIDTH = 600; // internal coordinate space; scales to container width via viewBox
const AXIS_FONT_SIZE = 10;
// Monospace-ish estimate (~0.6em/char at this font size) -- avoids a real DOM text-measurement
// pass just to size a margin. A fixed 44px left margin clipped wide labels off the left edge of
// the viewBox for large values (found live: llm_token_usage's real pre-fix values were in the
// billions, see agent/providers/ollama_cloud_llm.py's fix) -- the margin must scale with however
// wide the actual tick labels turn out to be, not assume they're always short.
const CHAR_WIDTH_ESTIMATE = AXIS_FONT_SIZE * 0.62;

function scale(domain: [number, number], range: [number, number]) {
  const [d0, d1] = domain;
  const [r0, r1] = range;
  return (v: number) => (d1 === d0 ? (r0 + r1) / 2 : r0 + ((v - d0) / (d1 - d0)) * (r1 - r0));
}

// Tiered suffixes, not just K -- a lone `/1000` step read raw millions-scale values (this
// project's own known pre-fix llm_token_usage historical data, see
// agent/providers/ollama_cloud_llm.py's fix) as a numeric-slop string like "10356442.1K"
// instead of "10.4M". Formatting the data these charts already receive; not a claim that the
// underlying numbers are new or corrected.
function defaultYFormat(v: number): string {
  const abs = Math.abs(v);
  if (abs >= 1_000_000_000) return `${(v / 1_000_000_000).toFixed(1)}B`;
  if (abs >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (abs >= 1_000) return `${(v / 1_000).toFixed(1)}K`;
  return Number.isInteger(v) ? String(v) : v.toFixed(2);
}

export function LineChart({ series, height = 200, yDomain, yFormat = defaultYFormat, emptyLabel }: LineChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [hoverX, setHoverX] = useState<number | null>(null);

  const hasData = series.some((s) => s.datapoints.length > 0);

  const { xDomain, yMin, yMax, allTimestamps } = useMemo(() => {
    const times = new Set<number>();
    let vMin = 0;
    let vMax = 0;
    for (const s of series) {
      for (const dp of s.datapoints) {
        const t = new Date(dp.timestamp).getTime();
        times.add(t);
        vMax = Math.max(vMax, dp.value);
        vMin = Math.min(vMin, dp.value);
      }
    }
    const sorted = [...times].sort((a, b) => a - b);
    const t0 = sorted[0] ?? Date.now() - 3600_000;
    const t1 = sorted[sorted.length - 1] ?? Date.now();
    return {
      xDomain: [t0, t1 === t0 ? t1 + 1 : t1] as [number, number],
      yMin: yDomain ? yDomain[0] : Math.min(0, vMin),
      yMax: yDomain ? yDomain[1] : vMax <= 0 ? 1 : vMax * 1.15,
      allTimestamps: sorted,
    };
  }, [series, yDomain]);

  const yTicks = 4;
  const tickValues = useMemo(
    () => Array.from({ length: yTicks + 1 }, (_, i) => yMin + ((yMax - yMin) * i) / yTicks),
    [yMin, yMax]
  );
  const widestLabelChars = useMemo(
    () => Math.max(0, ...tickValues.map((v) => yFormat(v).length)),
    [tickValues, yFormat]
  );
  const margin = useMemo(
    () => ({ ...BASE_MARGIN, left: Math.max(BASE_MARGIN.left, 12 + widestLabelChars * CHAR_WIDTH_ESTIMATE) }),
    [widestLabelChars]
  );

  const plotW = CHART_WIDTH - margin.left - margin.right;
  const plotH = height - margin.top - margin.bottom;
  const xScale = useMemo(
    () => scale(xDomain, [margin.left, margin.left + plotW]),
    [xDomain, margin.left, plotW]
  );
  const yScale = useMemo(
    () => scale([yMin, yMax], [margin.top + plotH, margin.top]),
    [yMin, yMax, margin.top, plotH]
  );

  if (!hasData) {
    return (
      <div
        className="flex items-center justify-center rounded-md border border-dashed text-sm text-muted-foreground"
        style={{ height }}
      >
        {emptyLabel ?? "No data yet for this window."}
      </div>
    );
  }

  const nearestTimestamp = (() => {
    if (hoverX === null || allTimestamps.length === 0) return null;
    let closest = allTimestamps[0];
    let best = Infinity;
    for (const t of allTimestamps) {
      const d = Math.abs(xScale(t) - hoverX);
      if (d < best) {
        best = d;
        closest = t;
      }
    }
    return closest;
  })();

  const handlePointerMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const rect = containerRef.current?.getBoundingClientRect();
    if (!rect) return;
    const relX = ((e.clientX - rect.left) / rect.width) * CHART_WIDTH;
    setHoverX(Math.max(margin.left, Math.min(margin.left + plotW, relX)));
  };

  return (
    <div className="flex flex-col gap-2">
      <div ref={containerRef} className="relative w-full" style={{ height }}>
        <svg
          viewBox={`0 0 ${CHART_WIDTH} ${height}`}
          preserveAspectRatio="none"
          width="100%"
          height={height}
          onPointerMove={handlePointerMove}
          onPointerLeave={() => setHoverX(null)}
          role="img"
          aria-label="Time series chart"
        >
          {/* Gridlines + y-axis labels */}
          {tickValues.map((v) => {
            const y = yScale(v);
            return (
              <g key={v}>
                <line
                  x1={margin.left}
                  x2={margin.left + plotW}
                  y1={y}
                  y2={y}
                  stroke="var(--chart-gridline)"
                  strokeWidth={1}
                />
                <text
                  x={margin.left - 8}
                  y={y}
                  textAnchor="end"
                  dominantBaseline="middle"
                  fontSize={10}
                  fontFamily="var(--font-mono)"
                  fill="var(--muted-foreground)"
                >
                  {yFormat(v)}
                </text>
              </g>
            );
          })}
          {/* Baseline */}
          <line
            x1={margin.left}
            x2={margin.left + plotW}
            y1={margin.top + plotH}
            y2={margin.top + plotH}
            stroke="var(--chart-baseline)"
            strokeWidth={1}
          />
          {/* X-axis labels: start / end */}
          <text
            x={margin.left}
            y={height - 6}
            fontSize={10}
            fontFamily="var(--font-mono)"
            fill="var(--muted-foreground)"
            textAnchor="start"
          >
            {new Date(xDomain[0]).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
          </text>
          <text
            x={margin.left + plotW}
            y={height - 6}
            fontSize={10}
            fontFamily="var(--font-mono)"
            fill="var(--muted-foreground)"
            textAnchor="end"
          >
            {new Date(xDomain[1]).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
          </text>

          {/* Series lines + end markers */}
          {series.map((s) => {
            if (s.datapoints.length === 0) return null;
            const sorted = [...s.datapoints].sort(
              (a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
            );
            const d = sorted
              .map((dp, idx) => {
                const x = xScale(new Date(dp.timestamp).getTime());
                const y = yScale(dp.value);
                return `${idx === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
              })
              .join(" ");
            const lastPoint = sorted[sorted.length - 1];
            const lastX = xScale(new Date(lastPoint.timestamp).getTime());
            const lastY = yScale(lastPoint.value);
            return (
              <g key={s.key}>
                <path d={d} fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
                <circle cx={lastX} cy={lastY} r={6} fill="var(--chart-surface)" />
                <circle cx={lastX} cy={lastY} r={4} fill={s.color} />
              </g>
            );
          })}

          {/* Hover crosshair -- flat, same hairline color as the baseline/gridlines */}
          {nearestTimestamp !== null && (
            <line
              x1={xScale(nearestTimestamp)}
              x2={xScale(nearestTimestamp)}
              y1={margin.top}
              y2={margin.top + plotH}
              stroke="var(--chart-baseline)"
              strokeWidth={1}
            />
          )}
        </svg>

        {/* Tooltip -- rendered as HTML (not SVG text) so it can sit above the chart and wrap.
            Flat: a hairline border, no shadow. */}
        {nearestTimestamp !== null && (
          <div
            className="pointer-events-none absolute top-2 z-10 flex flex-col gap-1 rounded-md border border-border bg-popover px-2.5 py-2 font-mono text-xs"
            style={{
              left: `${(xScale(nearestTimestamp) / CHART_WIDTH) * 100}%`,
              transform:
                xScale(nearestTimestamp) > CHART_WIDTH * 0.7 ? "translateX(-100%)" : "translateX(8px)",
            }}
          >
            <span className="text-muted-foreground">
              {new Date(nearestTimestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
            </span>
            {series.map((s) => {
              const dp = s.datapoints.find((d) => new Date(d.timestamp).getTime() === nearestTimestamp);
              if (!dp) return null;
              return (
                <div key={s.key} className="flex items-center gap-1.5">
                  <span className="inline-block h-0.5 w-3 rounded-full" style={{ backgroundColor: s.color }} />
                  <span className="font-semibold text-popover-foreground">{yFormat(dp.value)}</span>
                  <span className="font-sans text-muted-foreground">{s.label}</span>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {series.length > 1 && (
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
          {series.map((s) => (
            <span key={s.key} className="flex items-center gap-1.5">
              <span className="inline-block h-0.5 w-3 rounded-full" style={{ backgroundColor: s.color }} />
              {s.label}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
