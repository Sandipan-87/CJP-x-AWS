// references/marks-and-anatomy.md's stat-tile contract: label (sentence case, no trailing colon),
// value (semibold, auto-compact), optional trend sparkline in the de-emphasis hue. No delta here
// -- these metrics don't have a "vs a named period" baseline defined anywhere upstream, so adding
// one would be a fabricated comparison, not a real one.

interface StatTileProps {
  label: string;
  value: string;
  hint?: string;
}

export function StatTile({ label, value, hint }: StatTileProps) {
  return (
    <div className="flex flex-col gap-1 rounded-lg border border-border p-3">
      <span className="text-xs tracking-wider text-muted-foreground uppercase">{label}</span>
      <span className="font-mono text-3xl font-semibold">{value}</span>
      {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
    </div>
  );
}
