import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // src/lib/db.ts reads certs/memory-ca.crt via a dynamically-built fs path
  // (path.join(process.cwd(), ...)), not a static import -- Next's serverless
  // file tracing can't see that at build time, so the cert would silently be
  // dropped from the deployed function bundle without this explicit include.
  outputFileTracingIncludes: {
    "/api/**": ["./certs/**"],
  },
};

export default nextConfig;
