import type { NextRequest } from "next/server";

// design/02-low-level-design.md §11.2 / §12: "GET /metrics?window=1h -> CloudWatch GetMetricData."
// Same backend-for-frontend shape as src/app/api/approvals/[approvalId]/route.ts -- holds the API
// Gateway key SERVER-SIDE ONLY and proxies the browser's request to the real deployed endpoint,
// so the browser never sees the key. Reuses ENGRAM_APPROVALS_API_URL/_API_KEY rather than adding
// a second pair: both env vars are really "the one API Gateway's base URL/key," shared across
// every route under EngramApiStack's single usage plan (workers/metrics/handler.py's own route
// is `api_key_required=True` under that same plan) -- not approvals-specific despite the name.
export const dynamic = "force-dynamic";

export async function GET(request: NextRequest) {
  const apiUrl = process.env.ENGRAM_APPROVALS_API_URL;
  const apiKey = process.env.ENGRAM_APPROVALS_API_KEY;
  if (!apiUrl || !apiKey) {
    return Response.json(
      {
        error:
          "Metrics API not configured (ENGRAM_APPROVALS_API_URL/ENGRAM_APPROVALS_API_KEY unset) " +
          "-- the Lambda/API Gateway haven't been deployed yet. See infra/README.md.",
      },
      { status: 503 }
    );
  }

  const window = request.nextUrl.searchParams.get("window") ?? "1h";

  let upstream: Response;
  try {
    upstream = await fetch(
      `${apiUrl.replace(/\/$/, "")}/metrics?window=${encodeURIComponent(window)}`,
      { headers: { "X-Api-Key": apiKey }, cache: "no-store" }
    );
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return Response.json({ error: `upstream request failed: ${message}` }, { status: 502 });
  }

  const responseBody = await upstream.text();
  return new Response(responseBody, {
    status: upstream.status,
    headers: { "Content-Type": "application/json" },
  });
}
