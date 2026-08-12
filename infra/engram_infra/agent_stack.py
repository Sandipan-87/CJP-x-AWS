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

**EventBridge scope, deliberately narrow, stated not silently expanded**: CLAUDE.md §2's top-level
architecture line names three schedules feeding "SQS engram-commands" (5m sweep, 1h consolidate,
nightly decay), but LLD §9 independently describes consolidate/decay as separate, not-yet-built
lifecycle-worker LAMBDAS with no SQS/agent-graph involvement at all -- and CLAUDE.md's own "Next
action" list already tracks those as a distinct future item. This stack wires ONLY the 5-minute
sweep rule. Its target payload is a real, main.py-schema-valid EXAMPLE message (proven processable
by `scripts/smoke_test_main.py`'s own message shape) -- but the rule is created **disabled**
(`enabled=False`), because no sweep ENUMERATOR exists anywhere in this codebase yet (the thing that
would decide, every 5 minutes, WHICH scope/cluster/table/query is actually worth probing -- LLD
§5.1 step 1's still-unimplemented MCP/CloudWatch/ccloud collection legs). Firing a fixed example
message every 5 minutes forever would just manufacture a fake recurring "incident," not simulate
a real sweep. Enabling this rule for a live demo, or replacing its target with a real enumerator
Lambda, is real follow-up -- the plumbing exists and is provably correct, the decision logic
upstream of it does not yet.
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
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sqs as sqs
from constructs import Construct

DEFAULT_ECR_REPOSITORY_NAME = "engram-agent"
DEFAULT_SECRET_NAME = "engram/agent-secrets"
DEFAULT_IMAGE_TAG = "latest"
DEFAULT_HEALTH_PORT = 8080

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

# A real, main.py-schema-valid message (agent/main.py's module docstring, decision #6) --
# proven processable by scripts/smoke_test_main.py, not an invented shape. See module
# docstring for why the rule that would send this is created disabled.
EXAMPLE_SWEEP_MESSAGE = {
    "scope_id": "REPLACE-WITH-REAL-SCOPE-ID",
    "target_cluster_id": "REPLACE-WITH-REAL-TARGET-CLUSTER-UUID",
    "table_name": "REPLACE-WITH-REAL-TABLE",
    "query_text": "REPLACE-WITH-A-REAL-QUERY-TO-PROBE",
    "trigger": "eventbridge",
}


class EngramAgentStack(cdk.Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        ecr_repository_name: str = DEFAULT_ECR_REPOSITORY_NAME,
        secret_name: str = DEFAULT_SECRET_NAME,
        image_tag: str = DEFAULT_IMAGE_TAG,
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

        self._add_sweep_rule(queue)

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

    def _add_sweep_rule(self, queue: sqs.Queue) -> None:
        """See module docstring's "EventBridge scope" paragraph for why this is `enabled=False`."""
        events.Rule(
            self, "SweepRule",
            rule_name="engram-sweep-5min",
            schedule=events.Schedule.rate(Duration.minutes(5)),
            enabled=False,  # no sweep enumerator exists yet -- see module docstring
            targets=[
                targets.SqsQueue(
                    queue,
                    message_group_id="engram-sweep-placeholder",
                    message=events.RuleTargetInput.from_object(EXAMPLE_SWEEP_MESSAGE),
                )
            ],
        )
