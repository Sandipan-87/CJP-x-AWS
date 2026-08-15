// references/marks-and-anatomy.md's stat-tile contract: label (sentence case, no trailing colon),
// value (semibold, auto-compact), optional trend sparkline in the de-emphasis hue. No delta here
// -- these metrics don't have a "vs a named period" baseline defined anywhere upstream, so adding
// one would be a fabricated comparison, not a real one.

interface StatTileProps {
  label: string;
  value: string;
  hint?: string;
}

// No border/box here on purpose -- six identical bordered rectangles in a row is the
// "hero-metric template" the brutalist rules ban (a card grid standing in for a data table).
// The dividers between cells come from the parent grid's own divide-x/divide-y instead, so
// this reads as one flat stat strip, not six stacked mini-cards.
export function StatTile({ label, value, hint }: StatTileProps) {
  return (
    <div className="flex flex-col gap-1 px-3 py-2">
      <span className="text-xs tracking-wider text-muted-foreground uppercase">{label}</span>
      <span className="font-mono text-2xl font-semibold">{value}</span>
      {hint && <span className="text-xs text-muted-foreground">{hint}</span>}
    </div>
  );
}
