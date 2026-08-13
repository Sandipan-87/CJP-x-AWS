"""Engram · infra/engram_infra/agent_stack.py — SQS + EventBridge + ECS Fargate for agent/main.py.

design/02-low-level-design.md §1/§2/§4, HLD §5.5/§7.2/§10.2. The infra needed to actually run
`agent/main.py` (built + live-verified against real AWS/CockroachDB/Cohere/Ollama via
`scripts/smoke_test_main.py`, but never deployed -- CLAUDE.md's own OPEN list has said so since
that session) as a long-lived ECS Fargate task consuming a real SQS queue.

**Real, load-bearing constraint that shapes every choice below, stated up front**: this dev
environment has no Docker (`infra/README.md`'s own §5 already says so for the Lambda workers,
whose `pg8000`-pure-Python choice specifically avoided needing it -- `agent/`'s dependencies
include `psycopg[binary]` and, transitively via `langchain-cockroachdb`, `numpy`/
`psycopg2-binary`/`greenlet`, all native extensions that approach cannot avoid). So, unlike
`approvals_stack.py`, this stack does NOT build a container image itself
(`ecs.ContainerImage.from_asset()` needs local Docker) -- it IMPORTS an already-pushed image from
ECR by tag, mirroring the exact "CDK imports, something else provisions" split this project
already uses for Secrets Manager secrets:

  - **`.github/workflows/build-agent-image.yml`** (GitHub-hosted runners have Docker) builds and
    pushes the image, tagged `latest` and by git sha, into the ECR repo this stack imports.
  - **`scripts/bootstrap_agent_infra.py`** creates that ECR repo AND the `engram/agent-secrets`
    Secrets Manager secret (one JSON blob -- DSNs + API keys -- one ARN, matching this project's
    preference for fewer secrets/fewer IAM statements over one-secret-per-value) -- run once,
    under `engram-deploy`, same as every prior AWS-side provisioning gap in this project.

**Networking, decided here since nothing upstream specifies it**: a brand-new, dedicated,
`nat_gateways=0` VPC (`ec2.Vpc(..., nat_gateways=0)`), NOT `ec2.Vpc.from_lookup()` against the
account's default VPC. Two reasons: (1) `from_lookup()` needs a real AWS context lookup at synth
time, breaking `approvals_stack.py`'s "cdk synth needs no real AWS credentials" property this
stack would otherwise also have; (2) both CockroachDB clusters, Cohere, and Ollama Cloud are all
internet-reachable, not in any AWS VPC -- there is nothing to peer with privately. The Fargate
task runs in a PUBLIC subnet with `assign_public_ip=True` instead of a private subnet + NAT
Gateway, purely to avoid the NAT Gateway's ~$32/month minimum charge for a single always-on task
that needs outbound-only internet access anyway (CLAUDE.md §4's own "Free tier... budget paid
before rehearsal" cost-consciousness, applied to a new AWS service).

**No Application Load Balancer.** LLD §12's `GET /health` was written with an "ECS load balancer
target" in mind, but nothing here receives INBOUND traffic from users or browsers -- SQS is
pulled, not pushed, so there is no request to route. ECS's own container-level `healthCheck`
(running the same `python -c "urllib.request.urlopen(...)"` the Dockerfile's own `HEALTHCHECK`
uses) gets LLD's actual goal -- detect an unhealthy task, replace it -- without an ALB's real
ongoing cost for zero functional benefit here.

**EventBridge scope**: CLAUDE.md §2's top-level architecture line names three schedules feeding
"SQS engram-commands" (5m sweep, 1h consolidate, nightly decay), but LLD §9 independently
describes consolidate/decay/embedding_backfill as separate lifecycle-worker LAMBDAS with no SQS/
agent-graph involvement at all -- they never touch the queue this stack's own `AgentCommandsQueue`
guards. This stack wires the 5-minute sweep rule (`_add_sweep_rule`) AND, as of the session that
closed CLAUDE.md's own "lifecycle-worker Lambdas" Next-action item, the three §9 lifecycle rules
(`_add_lifecycle_rules`: 1h consolidate, nightly decay, nightly embedding backfill) -- all four
rules are created `enabled=False`, for the same "real, ongoing, unattended cost once enabled"
reason stated in each `_add_*_rule(s)` method's own docstring.

**The rule's target is now a real enumerator Lambda (`workers/sweep_enumerator/handler.py`), not a
fixed example message** -- CLAUDE.md's own Next-action list named this the actual blocker on ever
flipping the rule on, since firing one hardcoded message every 5 minutes forever would just
manufacture a fake recurring "incident," not simulate a real sweep. The enumerator reads
`db/migrations/008_watched_queries.sql`'s `watched_queries` registry (an explicit, ops-maintained
substitute for the LLD's own answer -- live MCP traffic discovery -- which this project has never
built an MCP client for at all, a separate, larger, already-tracked gap) and sends one real
`agent/main.py`-schema SQS message per enabled row. **The rule is still created `enabled=False`
here, deliberately, even though the enumerator itself is real and live-verified**: with an EMPTY
`watched_queries` registry (the default -- nothing seeds rows here or in any migration), enabling
the rule is functionally harmless (the enumerator runs, finds zero candidates, enqueues nothing,
costs one trivial Lambda invocation per tick) -- but flipping it on AND populating real rows
together starts a real, ONGOING, unattended cost (real Cohere/Ollama calls every time a watched
query trips the anomaly threshold, indefinitely, not a one-time action) -- exactly the kind of
consequential choice this project's own standing rule asks to confirm with the user first, not
decide unilaterally. Flipping `enabled=True` and/or seeding `watched_queries` rows is real,
available follow-up, not blocked on any remaining code.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import Duration
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from build import (
    build_consolidator_package,
    build_decayer_package,
    build_embedding_backfill_package,
    build_sweep_enumerator_package,
)

DEFAULT_ECR_REPOSITORY_NAME = "engram-agent"
DEFAULT_SECRET_NAME = "engram/agent-secrets"
DEFAULT_IMAGE_TAG = "latest"
DEFAULT_HEALTH_PORT = 8080
DEFAULT_SWEEP_DSN_SECRET_NAME = "engram/sweep-dsn"
DEFAULT_CONSOLIDATOR_DSN_SECRET_NAME = "engram/consolidator-dsn"
DEFAULT_DECAYER_DSN_SECRET_NAME = "engram/decayer-dsn"
DEFAULT_EMBEDDING_BACKFILL_DSN_SECRET_NAME = "engram/embedding-backfill-dsn"
DEFAULT_COHERE_SECRET_NAME = "engram/cohere-api-key"

# Every SECRET_ENV_KEYS field from scripts/bootstrap_agent_infra.py maps 1:1 to a container
# env var of the SAME name -- agent/main.py already reads each of these via plain os.environ.
SECRET_ENV_KEYS = (
    "ENGRAM_MEMORY_DSN",
    "ENGRAM_TARGET_PROBE_DSN",
    "ENGRAM_TARGET_OPERATOR_DSN",
    "ENGRAM_TARGET_DSN",
    "COHERE_API_KEY",
    "OLLAMA_API_KEY",
    "CCLOUD_TOKEN",
)

class EngramAgentStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        ecr_repository_name: str = DEFAULT_ECR_REPOSITORY_NAME,
        secret_name: str = DEFAULT_SECRET_NAME,
        image_tag: str = DEFAULT_IMAGE_TAG,
        sweep_dsn_secret_name: str = DEFAULT_SWEEP_DSN_SECRET_NAME,
        consolidator_dsn_secret_name: str = DEFAULT_CONSOLIDATOR_DSN_SECRET_NAME,
        decayer_dsn_secret_name: str = DEFAULT_DECAYER_DSN_SECRET_NAME,
        embedding_backfill_dsn_secret_name: str = DEFAULT_EMBEDDING_BACKFILL_DSN_SECRET_NAME,
        cohere_secret_name: str = DEFAULT_COHERE_SECRET_NAME,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        queue, dlq = self._add_queue()
        vpc = ec2.Vpc(self, "AgentVpc", max_azs=2, nat_gateways=0)  # see module docstring
        cluster = ecs.Cluster(self, "AgentCluster", cluster_name="engram-agent-cluster", vpc=vpc)

        repository = ecr.Repository.from_repository_name(self, "AgentRepository", ecr_repository_name)
        secret = secretsmanager.Secret.from_secret_name_v2(self, "AgentSecret", secret_name)

        task_definition = self._add_task_definition(repository, secret, queue, image_tag)
        service = ecs.FargateService(
            self, "AgentService",
            service_name="engram-agent",
            cluster=cluster,
            task_definition=task_definition,
            desired_count=1,  # LLD's own kill-and-resume demo beat: exactly one task
            assign_public_ip=True,  # public subnet, no NAT Gateway -- see module docstring
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            min_healthy_percent=0,  # a single-task service; ECS must be allowed to stop the
                                     # only task before starting its replacement, not block on it
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),  # cdk synth's own
                # annotation flagged this: without it, a task that can never start healthy (e.g.
                # deploying before the ECR image exists) can leave `cdk deploy` hanging for up to
                # 3 hours instead of failing fast and rolling back.
        )
        # Outbound-only by default (CDK's default security group already allows all egress,
        # no inbound rule added -- nothing needs to reach this task from outside the cluster).

        self._add_sweep_rule(queue, sweep_dsn_secret_name)
        self._add_lifecycle_rules(
            consolidator_dsn_secret_name, decayer_dsn_secret_name,
            embedding_backfill_dsn_secret_name, cohere_secret_name,
        )

        cdk.CfnOutput(self, "QueueUrl", value=queue.queue_url)
        cdk.CfnOutput(self, "DeadLetterQueueUrl", value=dlq.queue_url)
        cdk.CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        cdk.CfnOutput(self, "ServiceName", value=service.service_name)

    # ---------------------------------------------------------------------- queue

    def _add_queue(self) -> tuple[sqs.Queue, sqs.Queue]:
        # FIFO per HLD §5.5: "SQS FIFO message group = fingerprint" -- a publisher's concern
        # (message group id at send time), not agent/main.py's own consume_loop(), which reads
        # identically from a FIFO or standard queue either way.
        dlq = sqs.Queue(
            self, "AgentCommandsDlq",
            queue_name="engram-commands-dlq.fifo",
            fifo=True,
            retention_period=Duration.days(14),
        )
        queue = sqs.Queue(
            self, "AgentCommandsQueue",
            queue_name="engram-commands.fifo",
            fifo=True,
            content_based_deduplication=False,  # publishers set an explicit MessageDeduplicationId
            visibility_timeout=Duration.seconds(120),  # comfortably above a fast sweep's observe-only path;
                                                         # a full incident run is long-poll-independent since
                                                         # agent/main.py's own lease (60s TTL, 15s renew) is
                                                         # the real exactly-once mechanism, not this timeout
            retention_period=Duration.days(4),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=dlq),
        )
        return queue, dlq

    # ---------------------------------------------------------------------- task definition

    def _add_task_definition(
        self, repository: ecr.IRepository, secret: secretsmanager.ISecret, queue: sqs.Queue, image_tag: str,
    ) -> ecs.FargateTaskDefinition:
        task_definition = ecs.FargateTaskDefinition(
            self, "AgentTaskDefinition",
            family="engram-agent",
            cpu=512, memory_limit_mib=1024,  # I/O-bound (network calls to Cohere/Ollama/CockroachDB
                                               # Cloud), not compute-bound -- modest sizing on purpose
        )

        # Least-privilege IAM, same discipline as every SQL role and Lambda in this project.
        task_definition.add_to_task_role_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],  # CloudWatch's own genuine limitation -- PutMetricData does not
                                   # support resource-level ARN scoping, same documented exception
                                   # approvals_stack.py already records for GetMetricData/ListMetrics
            )
        )
        queue.grant_consume_messages(task_definition.task_role)
        secret.grant_read(task_definition.task_role)

        log_group = logs.LogGroup(
            self, "AgentLogGroup", log_group_name="/ecs/engram-agent", retention=logs.RetentionDays.TWO_WEEKS,
        )

        task_definition.add_container(
            "AgentContainer",
            image=ecs.ContainerImage.from_ecr_repository(repository, image_tag),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="engram-agent", log_group=log_group),
            environment={
                "AWS_REGION": self.region,
                "ENGRAM_QUEUE_URL": queue.queue_url,
                "ENGRAM_HEALTH_PORT": str(DEFAULT_HEALTH_PORT),
                "ENGRAM_APPROVAL_TIMEOUT_S": "600",
                "ENGRAM_LEASE_RENEW_S": "15",
                "ENGRAM_LLM_PROVIDER": "ollama",
                "OLLAMA_BASE_URL": "https://ollama.com",
                "ENGRAM_LLM_MODEL": "minimax-m3:cloud",
                "ENGRAM_EMBED_PROVIDER": "cohere",
                "ENGRAM_EMBED_MODEL": "embed-english-v3.0",
            },
            secrets={key: ecs.Secret.from_secrets_manager(secret, field=key) for key in SECRET_ENV_KEYS},
            port_mappings=[ecs.PortMapping(container_port=DEFAULT_HEALTH_PORT)],
            health_check=ecs.HealthCheck(
                command=[
                    "CMD-SHELL",
                    "python -c \"import urllib.request; "
                    f"urllib.request.urlopen('http://localhost:{DEFAULT_HEALTH_PORT}/health', timeout=4)\"",
                ],
                interval=Duration.seconds(30), timeout=Duration.seconds(5),
                retries=3, start_period=Duration.seconds(30),
            ),
        )

        # The EXECUTION role (not the task role) is what resolves `secrets=` at container
        # startup -- a separate grant, same distinction ECS itself draws between the two roles.
        # MUST come after add_container(): FargateTaskDefinition only lazily creates an
        # execution role once something (an ECR image + log driver, here) actually needs one --
        # `task_definition.execution_role` is `None` before that, a real ordering bug caught by
        # `cdk synth` itself on the first attempt (a `jsii` null-deserialization error), not
        # assumed correct.
        secret.grant_read(task_definition.execution_role)
        return task_definition

    # ---------------------------------------------------------------------- EventBridge

    def _add_sweep_rule(self, queue: sqs.Queue, sweep_dsn_secret_name: str) -> None:
        """See module docstring's "EventBridge scope" paragraph for why the rule itself is still
        `enabled=False` even though the enumerator Lambda it targets is real.
        """
        # Imports an ALREADY-EXISTING secret, same "CDK imports, something else provisions" split
        # as every other secret in this project -- scripts/bootstrap_sweep_enumerator_role.py
        # creates the DSN value, this stack never writes it.
        sweep_secret = secretsmanager.Secret.from_secret_name_v2(self, "SweepDsnSecret", sweep_dsn_secret_name)
        enumerator = lambda_.Function(
            self, "SweepEnumeratorFunction",
            function_name="engram-sweep-enumerator",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="sweep_enumerator.handler.handler",
            code=lambda_.Code.from_asset(build_sweep_enumerator_package()),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                "ENGRAM_SWEEP_SECRET_NAME": sweep_dsn_secret_name,
                "ENGRAM_QUEUE_URL": queue.queue_url,
            },
        )
        sweep_secret.grant_read(enumerator)
        queue.grant_send_messages(enumerator)

        events.Rule(
            self, "SweepRule",
            rule_name="engram-sweep-5min",
            schedule=events.Schedule.rate(Duration.minutes(5)),
            enabled=False,  # real enumerator now exists -- still off by default, see module docstring
            targets=[targets.LambdaFunction(enumerator)],
        )

    # ---------------------------------------------------------------------- lifecycle workers

    def _add_lifecycle_rules(
        self,
        consolidator_dsn_secret_name: str,
        decayer_dsn_secret_name: str,
        embedding_backfill_dsn_secret_name: str,
        cohere_secret_name: str,
    ) -> None:
        """design/02-low-level-design.md §9's three memory-janitor Lambdas -- `consolidator`
        (EventBridge 1h), `decayer` (nightly), `embedding_backfill` (nightly + on-demand; "on
        demand" needs no separate infra, any Lambda can already be invoked directly, e.g. via the
        console or CLI). Same "reuse the sweep-rule pattern" CLAUDE.md's own Next-action list
        asked for: each gets its own least-privilege DB role + DSN secret (migration 009's
        `engram_consolidator`/`engram_decayer`/`engram_embedding_backfill`, imported here by name,
        never created by this stack -- the same "CDK imports, something else provisions" split as
        every other secret in this project) and no VPC/queue access at all -- these only ever talk
        to CockroachDB Cloud and, for one of the three, `api.cohere.com`.

        **A single shared `engram/cohere-api-key` secret, not a copy inside each Lambda's own
        DSN secret**: only `embedding_backfill` actually needs it (one small, single-value secret,
        matching this project's `engram/sweep-dsn`/`engram/webhook-dsn` single-value convention).
        `consolidator` deliberately does NOT get it -- its own handler docstring (simplification
        #1) reuses already-stored embeddings rather than making a fresh Cohere call, and `decayer`
        never touches embeddings at all; granting either the secret would be an unused,
        speculative permission this project's least-privilege discipline argues against.

        **All three rules are created `enabled=False`, deliberately, same reasoning as
        `_add_sweep_rule`'s own rule**: `embedding_backfill` makes real, metered Cohere API calls
        every time it runs, and `consolidator`/`decayer` both write real `procedures`/
        `memory_items` rows -- turning any of these on starts a real, ONGOING, unattended cost/
        write pattern, exactly the kind of consequential choice this project's standing rule asks
        to confirm with the user first, not decide unilaterally. Flipping `enabled=True` for any
        of the three is real, available follow-up, not blocked on any remaining code.
        """
        consolidator_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "ConsolidatorDsnSecret", consolidator_dsn_secret_name,
        )
        decayer_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "DecayerDsnSecret", decayer_dsn_secret_name,
        )
        embedding_backfill_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "EmbeddingBackfillDsnSecret", embedding_backfill_dsn_secret_name,
        )
        cohere_secret = secretsmanager.Secret.from_secret_name_v2(self, "CohereApiKeySecret", cohere_secret_name)

        consolidator = lambda_.Function(
            self, "ConsolidatorFunction",
            function_name="engram-consolidator",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="consolidator.handler.handler",
            code=lambda_.Code.from_asset(build_consolidator_package()),
            timeout=Duration.seconds(300),  # LLD §9: "consolidator 300 s" -- the one Lambda here
                                              # with a non-default timeout, per-scope ANN clustering
                                              # over potentially many episodes
            memory_size=256,
            environment={
                "ENGRAM_CONSOLIDATOR_SECRET_NAME": consolidator_dsn_secret_name,
                # No COHERE_API_KEY_SECRET_NAME here -- handler.py's own docstring (simplification
                # #1) never makes a fresh embedding call, see migration 009's matching comment.
            },
        )
        consolidator_secret.grant_read(consolidator)

        decayer = lambda_.Function(
            self, "DecayerFunction",
            function_name="engram-decayer",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="decayer.handler.handler",
            code=lambda_.Code.from_asset(build_decayer_package()),
            timeout=Duration.seconds(60),
            memory_size=256,
            environment={"ENGRAM_DECAYER_SECRET_NAME": decayer_dsn_secret_name},
        )
        decayer_secret.grant_read(decayer)

        embedding_backfill = lambda_.Function(
            self, "EmbeddingBackfillFunction",
            function_name="engram-embedding-backfill",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="embedding_backfill.handler.handler",
            code=lambda_.Code.from_asset(build_embedding_backfill_package()),
            timeout=Duration.seconds(60),
            memory_size=256,
            environment={
                "ENGRAM_EMBEDDING_BACKFILL_SECRET_NAME": embedding_backfill_dsn_secret_name,
                "COHERE_API_KEY_SECRET_NAME": cohere_secret_name,
            },
        )
        embedding_backfill_secret.grant_read(embedding_backfill)
        cohere_secret.grant_read(embedding_backfill)

        events.Rule(
            self, "ConsolidateRule",
            rule_name="engram-consolidate-1h",
            schedule=events.Schedule.rate(Duration.hours(1)),  # LLD §9: "EventBridge 1 h"
            enabled=False,  # see module note above
            targets=[targets.LambdaFunction(consolidator)],
        )
        events.Rule(
            self, "DecayRule",
            rule_name="engram-decay-nightly",
            schedule=events.Schedule.rate(Duration.days(1)),  # LLD §9: "EventBridge nightly"
            enabled=False,
            targets=[targets.LambdaFunction(decayer)],
        )
        events.Rule(
            self, "EmbeddingBackfillRule",
            rule_name="engram-embedding-backfill-nightly",
            schedule=events.Schedule.rate(Duration.days(1)),  # LLD §9: "nightly + on-demand" --
                                                                 # "on-demand" needs no separate
                                                                 # infra, see method docstring
            enabled=False,
            targets=[targets.LambdaFunction(embedding_backfill)],
        )
