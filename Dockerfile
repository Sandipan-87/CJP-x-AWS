# Engram · agent/main.py's ECS Fargate runtime image.  [PLUMBER]
#
# infra/README.md's own §5 already states why this can't reuse infra/build.py's
# Docker-avoidance trick (`pip install --target`, safe only for pure-Python deps):
# agent/requirements.txt pulls psycopg[binary] (a native extension), plus numpy/
# psycopg2-binary/greenlet transitively via langchain-cockroachdb -- a real container
# image is unavoidable here, unlike the Lambda workers. This image is built by
# .github/workflows/build-agent-image.yml (GitHub-hosted runners have Docker; this
# dev environment does not -- confirmed directly, `docker` isn't even on PATH here),
# NOT by `cdk deploy` itself -- infra/engram_infra/agent_stack.py imports an
# ALREADY-PUSHED image from ECR by tag, the same "CDK imports, something else
# provisions" split this project already uses for Secrets Manager secrets.

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ agent/
# The one CA file confirmed (2026-08-12) to work for BOTH clusters in this org --
# see agent/main.py's own smoke test and .env.example's ENGRAM_*_SSLROOTCERT comment.
COPY workers/common/certs/memory-ca.crt /app/certs/memory-ca.crt

ENV PYTHONUNBUFFERED=1 \
    ENGRAM_MEMORY_SSLROOTCERT=/app/certs/memory-ca.crt \
    ENGRAM_TARGET_SSLROOTCERT=/app/certs/memory-ca.crt

EXPOSE 8080

# Native health check, no extra OS package (no curl) -- ECS's own container-level
# healthCheck (infra/engram_infra/agent_stack.py) runs exactly this command.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8080/health', timeout=4)"]

CMD ["python", "-m", "agent.main"]
