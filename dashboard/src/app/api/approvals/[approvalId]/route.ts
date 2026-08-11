import type { NextRequest } from "next/server";

// design/02-low-level-design.md §11.2 / HLD §5.6: "Mutations (approve/reject) -> API Gateway ->
// Lambda -> memory cluster." This route is the backend-for-frontend hop in that chain -- it
// holds the API Gateway key SERVER-SIDE ONLY (ENGRAM_APPROVALS_API_KEY, never sent to the
// browser) and proxies the browser's request to the real endpoint. The browser itself never
// talks to API Gateway directly, and never sees the key -- same reasoning as why the SSE routes
// (src/lib/db.ts) hold engram_reader server-side only.
export const dynamic = "force-dynamic";

export async function POST(
  request: NextRequest,
  { params }: { params: Promise<{ approvalId: string }> }
) {
  const { approvalId } = await params;

  const apiUrl = process.env.ENGRAM_APPROVALS_API_URL;
  const apiKey = process.env.ENGRAM_APPROVALS_API_KEY;
  if (!apiUrl || !apiKey) {
    return Response.json(
      {
        error:
          "Approvals API not configured (ENGRAM_APPROVALS_API_URL/ENGRAM_APPROVALS_API_KEY unset) " +
          "-- the Lambda/API Gateway haven't been deployed yet. See infra/README.md.",
      },
      { status: 503 }
    );
  }

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return Response.json({ error: "body must be valid JSON" }, { status: 400 });
  }

  let upstream: Response;
  try {
    upstream = await fetch(`${apiUrl.replace(/\/$/, "")}/approvals/${approvalId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Api-Key": apiKey },
      body: JSON.stringify(body),
    });
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
