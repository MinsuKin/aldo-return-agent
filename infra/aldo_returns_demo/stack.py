"""AldoReturnsDemo: base infrastructure for the Agentic Returns Gateway prototype.

Implements design.md section 9, the infra-relevant design-gap findings and the
PIVOT_ADDENDUM (ALDO Fit Gateway) deltas:
  G-05  worker async retries off (retry_attempts=0, short max_event_age)
  G-09  SPA fallback via a CloudFront Function on the default behavior only
        (no distribution-level custom error responses, which would rewrite API
        401/403/404 JSON bodies)
  G-10  Lambda dependencies are bundled (no layer), arm64 / Python 3.12
  G-20  runtime ARN is a deploy-time context value with a safe placeholder
  P-a   second table aldo-personal-agent-demo (personal agent only; the ALDO
        AgentCore execution role gets NO access to it)
  P-b   generated writeback_key in aldo-demo/secrets
  P-c   gateway URL published to SSM /aldo-demo/gateway-url (not a token env var:
        that would create a cycle worker -> HTTP API -> API Lambda -> worker)
  P-d   CloudFront /.well-known/* behavior to the HTTP API origin

Lambda code comes from infra/stubs by default. Context flag ``useRealCode=true``
switches both functions to infra/build/lambda, which scripts/bundle_backend.sh
--lambda builds from backend/ (returns_domain, personal_agent, api, worker,
fixtures_bundle) plus arm64 dependencies.
"""
from __future__ import annotations

import json
from pathlib import Path

from aws_cdk import (
    ArnFormat,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigw_integrations,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
    aws_ssm as ssm,
)
from constructs import Construct

INFRA_DIR = Path(__file__).resolve().parent.parent
STUBS_DIR = INFRA_DIR / "stubs"
REAL_CODE_DIR = INFRA_DIR / "build" / "lambda"

TABLE_NAME = "aldo-returns-demo"
PERSONAL_AGENT_TABLE_NAME = "aldo-personal-agent-demo"
SECRET_NAME = "aldo-demo/secrets"
DEMO_NOW = "2026-10-16T12:00:00-04:00"
API_FUNCTION_NAME = "aldo-returns-demo-api"
WORKER_FUNCTION_NAME = "aldo-returns-demo-worker"
SPA_FUNCTION_NAME = "aldo-returns-demo-spa-rewrite"
RUNTIME_ARN_PARAM = "/aldo-demo/runtime-arn"
GATEWAY_URL_PARAM = "/aldo-demo/gateway-url"
DELEGATION_MAX_CENTS = "25000"
# Directory inside the Lambda package that holds the bundled fixtures.
LAMBDA_FIXTURES_DIR = "/var/task/fixtures_bundle"

# Viewer-request function for the DEFAULT (S3) behavior only. Extension-less
# paths are client-side routes and get index.html. Paths with a file extension
# (hashed assets, favicon) pass through; a missing asset stays a plain S3
# error and is never rewritten.
SPA_REWRITE_JS = """\
function handler(event) {
  var request = event.request;
  var uri = request.uri;
  var lastSegment = uri.substring(uri.lastIndexOf('/') + 1);
  if (lastSegment.indexOf('.') === -1) {
    request.uri = '/index.html';
  }
  return request;
}
"""


class AldoReturnsDemoStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        use_real_code: bool = False,
        runtime_arn: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("project", "aldo-returns-demo")

        # ------------------------------------------------------------------
        # Data: DynamoDB single table (design section 3) and the personal
        # agent's own memory table (pivot P-a).
        # ------------------------------------------------------------------
        table = dynamodb.Table(
            self,
            "Table",
            table_name=TABLE_NAME,
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )
        table.add_global_secondary_index(
            index_name="GSI1",
            partition_key=dynamodb.Attribute(name="gsi1pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="gsi1sk", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )
        personal_table = dynamodb.Table(
            self,
            "PersonalAgentTable",
            table_name=PERSONAL_AGENT_TABLE_NAME,
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ------------------------------------------------------------------
        # Secrets Manager: one secret with independently generated keys.
        # SecretStringGenerator can only generate a single key, so one
        # generator secret per key feeds secret_object_value through dynamic
        # references (verified on this account with a throwaway stack).
        # Keys: token_key (session + delegated tokens), admin_key (reset),
        # writeback_key (ALDO -> personal agent HMAC signature).
        # ------------------------------------------------------------------
        def generated_key(construct_id_: str) -> secretsmanager.Secret:
            return secretsmanager.Secret(
                self,
                construct_id_,
                generate_secret_string=secretsmanager.SecretStringGenerator(
                    password_length=48,
                    exclude_punctuation=True,
                ),
                removal_policy=RemovalPolicy.DESTROY,
            )

        token_key_gen = generated_key("TokenKeyGen")
        admin_key_gen = generated_key("AdminKeyGen")
        writeback_key_gen = generated_key("WritebackKeyGen")
        secret = secretsmanager.Secret(
            self,
            "AppSecret",
            secret_name=SECRET_NAME,
            description="Demo secrets (JSON: token_key, admin_key, writeback_key)",
            secret_object_value={
                "token_key": token_key_gen.secret_value,
                "admin_key": admin_key_gen.secret_value,
                "writeback_key": writeback_key_gen.secret_value,
            },
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ------------------------------------------------------------------
        # Shared ARNs / statements
        # ------------------------------------------------------------------
        runtime_arn_value = runtime_arn or self.format_arn(
            service="bedrock-agentcore",
            resource="runtime",
            resource_name="aldo_placeholder_not_deployed",
        )
        gateway_url_param_arn = self.format_arn(
            service="ssm",
            resource="parameter",
            resource_name=GATEWAY_URL_PARAM.lstrip("/"),
            arn_format=ArnFormat.SLASH_RESOURCE_NAME,
        )
        bedrock_invoke = iam.PolicyStatement(
            sid="InvokeClaudeModels",
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=[
                "arn:aws:bedrock:*::foundation-model/anthropic.*",
                self.format_arn(
                    service="bedrock",
                    resource="inference-profile",
                    resource_name="us.anthropic.*",
                ),
            ],
        )

        # ------------------------------------------------------------------
        # Lambdas
        # ------------------------------------------------------------------
        code = self._lambda_code(use_real_code)
        common_env = {
            "TABLE_NAME": table.table_name,
            "PERSONAL_AGENT_TABLE_NAME": personal_table.table_name,
            "DEMO_NOW": DEMO_NOW,
            # returns_domain.config reads SECRETS_ARN (a name or an ARN; JSON token_key, admin_key).
            "SECRETS_ARN": secret.secret_arn,
            "SECRET_NAME": SECRET_NAME,
            "FIXTURES_DIR": LAMBDA_FIXTURES_DIR,
            "GATEWAY_TRANSPORT": "http",
            # Name of the SSM parameter that holds the gateway base URL; read at runtime.
            "GATEWAY_BASE_URL_PARAM": GATEWAY_URL_PARAM,
            "DELEGATION_MAX_CENTS": DELEGATION_MAX_CENTS,
            "POWERTOOLS_LOG_LEVEL": "INFO",
            "LOG_LEVEL": "INFO",
        }

        worker_logs = logs.LogGroup(
            self,
            "WorkerLogs",
            log_group_name=f"/aws/lambda/{WORKER_FUNCTION_NAME}",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        worker_fn = lambda_.Function(
            self,
            "WorkerFn",
            function_name=WORKER_FUNCTION_NAME,
            description="Runs one agent run: ALDO agent via AgentCore runtime, personal agent in-process",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="worker.handler.handler",
            code=code,
            # Strands is imported in-process for the personal agent: more memory
            # means more CPU and a faster cold start.
            memory_size=1024,
            timeout=Duration.seconds(300),
            environment={
                **common_env,
                "POWERTOOLS_SERVICE_NAME": "returns-worker",
                "AGENT_MODE": "agentcore",
                # Exact ARN (also the only runtime the worker role may invoke). Set from the
                # SSM parameter by infra/deploy.sh; a placeholder until the runtime exists.
                "AGENT_RUNTIME_ARN": runtime_arn_value,
            },
            log_group=worker_logs,
            # G-05: async invocation retries would start duplicate agent runs.
            retry_attempts=0,
            max_event_age=Duration.minutes(2),
        )

        api_logs = logs.LogGroup(
            self,
            "ApiLogs",
            log_group_name=f"/aws/lambda/{API_FUNCTION_NAME}",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        api_fn = lambda_.Function(
            self,
            "ApiFn",
            function_name=API_FUNCTION_NAME,
            description="HTTP API handler: auth, orders, quotes, approve, runs, gateway, reset",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="api.handler.handler",
            code=code,
            memory_size=512,
            timeout=Duration.seconds(15),
            environment={
                **common_env,
                "POWERTOOLS_SERVICE_NAME": "returns-api",
                # Plain string, not worker_fn.function_name: keeps the API Lambda
                # free of a token reference to the worker (pivot P-c).
                "WORKER_FUNCTION_NAME": WORKER_FUNCTION_NAME,
            },
            log_group=api_logs,
        )

        # ------------------------------------------------------------------
        # IAM (least privilege)
        # ------------------------------------------------------------------
        for fn in (api_fn, worker_fn):
            table.grant_read_write_data(fn)
            personal_table.grant_read_write_data(fn)
            secret.grant_read(fn)
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    sid="ReadGatewayUrlParameter",
                    actions=["ssm:GetParameter"],
                    resources=[gateway_url_param_arn],
                )
            )
        worker_fn.grant_invoke(api_fn)
        worker_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="InvokeAgentRuntime",
                effect=iam.Effect.ALLOW,
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[runtime_arn_value, f"{runtime_arn_value}/runtime-endpoint/*"],
            )
        )
        # The personal agent runs in-process in the worker (pivot 5.7) and calls Bedrock.
        worker_fn.add_to_role_policy(bedrock_invoke)

        # Extra permissions for the ALDO AgentCore runtime execution role. The
        # starter toolkit creates the role; the agent deploy step attaches this
        # managed policy to it. Deliberately NO access to the personal agent
        # table (pivot P-a): the ALDO agent must not read the owner's memory.
        agent_extra_policy = iam.ManagedPolicy(
            self,
            "AgentRuntimeExtraPolicy",
            managed_policy_name="aldo-returns-demo-agent-runtime-extra",
            description="Returns table access and Bedrock invoke for the ALDO AgentCore runtime execution role",
            statements=[
                iam.PolicyStatement(
                    sid="DemoTableReadWrite",
                    actions=[
                        "dynamodb:BatchGetItem",
                        "dynamodb:GetItem",
                        "dynamodb:Query",
                        "dynamodb:Scan",
                        "dynamodb:ConditionCheckItem",
                        "dynamodb:DescribeTable",
                        "dynamodb:BatchWriteItem",
                        "dynamodb:PutItem",
                        "dynamodb:UpdateItem",
                        "dynamodb:DeleteItem",
                    ],
                    resources=[table.table_arn, f"{table.table_arn}/index/*"],
                ),
                bedrock_invoke,
            ],
        )

        # ------------------------------------------------------------------
        # HTTP API: $default route to the API Lambda, throttled
        # ------------------------------------------------------------------
        http_api = apigwv2.HttpApi(
            self,
            "HttpApi",
            api_name="aldo-returns-demo",
            description="Agentic Returns Gateway demo API (reached through CloudFront /api/* and /.well-known/*)",
            create_default_stage=False,
            default_integration=apigw_integrations.HttpLambdaIntegration("ApiIntegration", api_fn),
        )
        stage = apigwv2.HttpStage(
            self,
            "DefaultStage",
            http_api=http_api,
            stage_name="$default",
            auto_deploy=True,
            throttle=apigwv2.ThrottleSettings(rate_limit=20, burst_limit=40),
        )

        # ------------------------------------------------------------------
        # Frontend: private S3 bucket + CloudFront (OAC)
        # ------------------------------------------------------------------
        bucket = s3.Bucket(
            self,
            "SiteBucket",
            bucket_name=f"aldo-returns-demo-site-{self.account}-{self.region}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        spa_rewrite = cloudfront.Function(
            self,
            "SpaRewrite",
            function_name=SPA_FUNCTION_NAME,
            comment="Rewrite extension-less SPA routes to /index.html (default behavior only)",
            runtime=cloudfront.FunctionRuntime.JS_2_0,
            code=cloudfront.FunctionCode.from_inline(SPA_REWRITE_JS),
        )

        api_origin = origins.HttpOrigin(
            f"{http_api.api_id}.execute-api.{self.region}.{self.url_suffix}",
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
            read_timeout=Duration.seconds(30),
        )
        # Identical settings for every HTTP API path: no cache, all methods, all
        # viewer headers except Host, no function association.
        api_behavior = cloudfront.BehaviorOptions(
            origin=api_origin,
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
            cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
        )
        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            comment="aldo-returns-demo",
            default_root_object="index.html",
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                compress=True,
                function_associations=[
                    cloudfront.FunctionAssociation(
                        function=spa_rewrite,
                        event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                    )
                ],
            ),
            additional_behaviors={
                "/api/*": api_behavior,
                # Agent card (pivot P-d): /.well-known/agent-card.json
                "/.well-known/*": api_behavior,
            },
            # Deliberately no error_responses: distribution-level custom error
            # responses would rewrite the API's 401/403/404 JSON bodies.
        )
        # CloudFront takes 5-15 min to create. Create it after the cheaper
        # resources so a failure in any of them rolls back before the
        # distribution exists. (Not the bucket: its policy references the
        # distribution, which would make a cycle.)
        distribution.node.add_dependency(
            table,
            personal_table,
            secret,
            worker_fn,
            api_fn,
            stage,
            spa_rewrite,
            agent_extra_policy,
        )

        # Gateway base URL for the personal agent's http transport and the agent
        # card. Published to SSM instead of a token env var (pivot P-c); the
        # worker and API read it at runtime.
        gateway_url_param = ssm.StringParameter(
            self,
            "GatewayUrlParameter",
            parameter_name=GATEWAY_URL_PARAM,
            description="Public base URL of the ALDO gateway (CloudFront), no trailing slash",
            string_value=f"https://{distribution.distribution_domain_name}",
        )

        # ------------------------------------------------------------------
        # Outputs (non-secret)
        # ------------------------------------------------------------------
        CfnOutput(self, "CloudFrontUrl", value=f"https://{distribution.distribution_domain_name}")
        CfnOutput(self, "DistributionId", value=distribution.distribution_id)
        CfnOutput(self, "ApiUrl", value=f"https://{http_api.api_id}.execute-api.{self.region}.{self.url_suffix}/")
        CfnOutput(self, "TableName", value=table.table_name)
        CfnOutput(self, "PersonalAgentTableName", value=personal_table.table_name)
        CfnOutput(self, "BucketName", value=bucket.bucket_name)
        CfnOutput(self, "SecretName", value=SECRET_NAME)
        CfnOutput(self, "ApiFunctionName", value=api_fn.function_name)
        CfnOutput(self, "WorkerFunctionName", value=worker_fn.function_name)
        # Only this role may invoke the AgentCore runtime (requirement 1.5).
        CfnOutput(self, "WorkerRoleArn", value=worker_fn.role.role_arn)
        CfnOutput(self, "AgentRuntimeExtraPolicyArn", value=agent_extra_policy.managed_policy_arn)
        CfnOutput(self, "RuntimeArnParameter", value=RUNTIME_ARN_PARAM)
        CfnOutput(self, "GatewayUrlParameterName", value=gateway_url_param.parameter_name)
        CfnOutput(self, "CodeMode", value="real" if use_real_code else "stub")

    @staticmethod
    def _lambda_code(use_real_code: bool) -> lambda_.Code:
        exclude = ["**/__pycache__", "**/*.pyc", ".deps-hash"]
        if not use_real_code:
            return lambda_.Code.from_asset(str(STUBS_DIR), exclude=exclude)
        missing = [
            str(REAL_CODE_DIR / rel)
            for rel in ("api/handler.py", "worker/handler.py")
            if not (REAL_CODE_DIR / rel).exists()
        ]
        if missing:
            raise ValueError(
                "useRealCode=true but the Lambda bundle is missing "
                + json.dumps(missing)
                + ". Run scripts/bundle_backend.sh --lambda first."
            )
        return lambda_.Code.from_asset(str(REAL_CODE_DIR), exclude=exclude)
