"""Assertions on the synthesized AldoReturnsDemo template. No AWS access needed."""
import json

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from aldo_returns_demo import stack as stack_module
from aldo_returns_demo.stack import AldoReturnsDemoStack

ACCOUNT = "111111111111"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:runtime/aldo_returns_agent-AbCdEfGhIj"
CACHING_DISABLED_ID = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
ALL_VIEWER_EXCEPT_HOST_ID = "b689b0a8-53d0-40ab-baf2-68738e2966ac"


def _synth(**kwargs) -> Template:
    app = cdk.App()
    stack = AldoReturnsDemoStack(
        app,
        "AldoReturnsDemo",
        env=cdk.Environment(account=ACCOUNT, region="us-east-1"),
        **kwargs,
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def template() -> Template:
    return _synth()


def _only(template: Template, resource_type: str) -> dict:
    found = template.find_resources(resource_type)
    assert len(found) == 1, f"expected exactly one {resource_type}, got {list(found)}"
    return next(iter(found.values()))


def _function(template: Template, name: str) -> dict:
    for resource in template.find_resources("AWS::Lambda::Function").values():
        if resource["Properties"].get("FunctionName") == name:
            return resource
    raise AssertionError(f"Lambda {name} not found")


def _statements(template: Template, role_prefix: str) -> list[dict]:
    """All IAM statements attached (inline policies) to the role whose logical id starts with role_prefix."""
    role_ids = [rid for rid in template.find_resources("AWS::IAM::Role") if rid.startswith(role_prefix)]
    assert len(role_ids) == 1, role_ids
    statements: list[dict] = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        if {"Ref": role_ids[0]} in policy["Properties"]["Roles"]:
            statements.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return statements


def _actions(statements: list[dict]) -> set[str]:
    actions: set[str] = set()
    for statement in statements:
        value = statement["Action"]
        actions.update([value] if isinstance(value, str) else value)
    return actions


def test_tables(template: Template) -> None:
    tables = {t["Properties"]["TableName"]: t for t in template.find_resources("AWS::DynamoDB::Table").values()}
    assert set(tables) == {"aldo-returns-demo", "aldo-personal-agent-demo"}
    main = tables["aldo-returns-demo"]["Properties"]
    assert main["BillingMode"] == "PAY_PER_REQUEST"
    assert main["KeySchema"] == [
        {"AttributeName": "pk", "KeyType": "HASH"},
        {"AttributeName": "sk", "KeyType": "RANGE"},
    ]
    (gsi,) = main["GlobalSecondaryIndexes"]
    assert gsi["IndexName"] == "GSI1"
    assert gsi["KeySchema"] == [
        {"AttributeName": "gsi1pk", "KeyType": "HASH"},
        {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
    ]
    personal = tables["aldo-personal-agent-demo"]["Properties"]
    assert personal["BillingMode"] == "PAY_PER_REQUEST"
    assert "GlobalSecondaryIndexes" not in personal
    for table in tables.values():
        assert table["DeletionPolicy"] == "Delete"


def test_secret_has_three_generated_keys(template: Template) -> None:
    secrets = [
        r for r in template.find_resources("AWS::SecretsManager::Secret").values() if r["Properties"].get("Name")
    ]
    (secret,) = secrets
    assert secret["Properties"]["Name"] == "aldo-demo/secrets"
    rendered = json.dumps(secret["Properties"]["SecretString"])
    for key in ("token_key", "admin_key", "writeback_key"):
        assert key in rendered
    assert rendered.count("resolve:secretsmanager") == 3


def test_no_distribution_level_custom_error_responses(template: Template) -> None:
    # They would also rewrite the API's 401/403/404 JSON bodies into index.html.
    config = _only(template, "AWS::CloudFront::Distribution")["Properties"]["DistributionConfig"]
    assert "CustomErrorResponses" not in config
    assert config["DefaultRootObject"] == "index.html"


def test_spa_function_is_only_on_the_default_behavior(template: Template) -> None:
    config = _only(template, "AWS::CloudFront::Distribution")["Properties"]["DistributionConfig"]
    default_associations = config["DefaultCacheBehavior"]["FunctionAssociations"]
    assert len(default_associations) == 1
    assert default_associations[0]["EventType"] == "viewer-request"
    behaviors = {b["PathPattern"]: b for b in config["CacheBehaviors"]}
    assert set(behaviors) == {"/api/*", "/.well-known/*"}
    for behavior in behaviors.values():
        assert "FunctionAssociations" not in behavior
    function = _only(template, "AWS::CloudFront::Function")["Properties"]
    assert function["FunctionConfig"]["Runtime"] == "cloudfront-js-2.0"
    assert "/index.html" in function["FunctionCode"]


def test_api_and_well_known_behaviors_pass_everything_to_the_http_api(template: Template) -> None:
    config = _only(template, "AWS::CloudFront::Distribution")["Properties"]["DistributionConfig"]
    behaviors = {b["PathPattern"]: b for b in config["CacheBehaviors"]}
    assert behaviors["/api/*"] == {**behaviors["/.well-known/*"], "PathPattern": "/api/*"}
    api = behaviors["/api/*"]
    assert api["CachePolicyId"] == CACHING_DISABLED_ID
    assert api["OriginRequestPolicyId"] == ALL_VIEWER_EXCEPT_HOST_ID
    assert set(api["AllowedMethods"]) == {"GET", "HEAD", "OPTIONS", "PUT", "PATCH", "POST", "DELETE"}
    assert api["ViewerProtocolPolicy"] == "https-only"
    origins = {o["Id"]: o for o in config["Origins"]}
    assert "CustomOriginConfig" in origins[api["TargetOriginId"]]
    assert "S3OriginConfig" in origins[config["DefaultCacheBehavior"]["TargetOriginId"]]


def test_http_api_default_route_and_throttle(template: Template) -> None:
    template.has_resource_properties("AWS::ApiGatewayV2::Route", {"RouteKey": "$default"})
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        {
            "StageName": "$default",
            "AutoDeploy": True,
            "DefaultRouteSettings": {"ThrottlingBurstLimit": 40, "ThrottlingRateLimit": 20},
        },
    )


def test_lambda_configuration(template: Template) -> None:
    api = _function(template, "aldo-returns-demo-api")["Properties"]
    worker = _function(template, "aldo-returns-demo-worker")["Properties"]
    for props in (api, worker):
        assert props["Runtime"] == "python3.12"
        assert props["Architectures"] == ["arm64"]
    assert (api["MemorySize"], api["Timeout"], api["Handler"]) == (512, 15, "api.handler.handler")
    assert (worker["MemorySize"], worker["Timeout"], worker["Handler"]) == (1024, 300, "worker.handler.handler")
    # Duplicate agent runs: async retries must be off for the worker (design gap G-05).
    config = _only(template, "AWS::Lambda::EventInvokeConfig")["Properties"]
    assert config["MaximumRetryAttempts"] == 0
    assert config["MaximumEventAgeInSeconds"] <= 300


def test_lambda_log_groups_keep_logs_for_a_week(template: Template) -> None:
    groups = {g["Properties"]["LogGroupName"]: g for g in template.find_resources("AWS::Logs::LogGroup").values()}
    assert {"/aws/lambda/aldo-returns-demo-api", "/aws/lambda/aldo-returns-demo-worker"} <= set(groups)
    for name in ("/aws/lambda/aldo-returns-demo-api", "/aws/lambda/aldo-returns-demo-worker"):
        assert groups[name]["Properties"]["RetentionInDays"] == 7


def test_lambda_environment(template: Template) -> None:
    api = _function(template, "aldo-returns-demo-api")["Properties"]["Environment"]["Variables"]
    worker = _function(template, "aldo-returns-demo-worker")["Properties"]["Environment"]["Variables"]
    for env in (api, worker):
        assert env["DEMO_NOW"] == "2026-10-16T12:00:00-04:00"
        assert env["SECRET_NAME"] == "aldo-demo/secrets"
        assert env["FIXTURES_DIR"] == "/var/task/fixtures_bundle"
        assert env["GATEWAY_TRANSPORT"] == "http"
        assert env["DELEGATION_MAX_CENTS"] == "25000"
        # Name of the SSM parameter (a plain string); the code resolves the URL at runtime.
        assert env["GATEWAY_BASE_URL_PARAM"] == "/aldo-demo/gateway-url"
        assert "TABLE_NAME" in env and "PERSONAL_AGENT_TABLE_NAME" in env
        # returns_domain.config reads the secret through SECRETS_ARN.
        assert "SECRETS_ARN" in env
        # The gateway URL itself must never be a CDK token env var (cycle worker -> API -> worker).
        assert "GATEWAY_BASE_URL" not in env
        assert not any("execute-api" in json.dumps(value) or "cloudfront" in json.dumps(value).lower() for value in env.values())
    # Plain string, so the API Lambda carries no token reference to the worker.
    assert api["WORKER_FUNCTION_NAME"] == "aldo-returns-demo-worker"
    assert "AGENT_RUNTIME_ARN" in worker
    assert "aldo_placeholder_not_deployed" in json.dumps(worker["AGENT_RUNTIME_ARN"])


def test_gateway_url_is_published_to_ssm(template: Template) -> None:
    template.has_resource_properties("AWS::SSM::Parameter", {"Name": "/aldo-demo/gateway-url", "Type": "String"})


def test_api_role_permissions(template: Template) -> None:
    statements = _statements(template, "ApiFnServiceRole")
    actions = _actions(statements)
    assert "lambda:InvokeFunction" in actions
    assert "secretsmanager:GetSecretValue" in actions
    assert "dynamodb:PutItem" in actions
    # The API must not be able to start the agent runtime or call Bedrock.
    assert "bedrock-agentcore:InvokeAgentRuntime" not in actions
    assert not any(a.startswith("bedrock:") for a in actions)


def test_worker_role_permissions(template: Template) -> None:
    statements = _statements(template, "WorkerFnServiceRole")
    actions = _actions(statements)
    assert {"secretsmanager:GetSecretValue", "ssm:GetParameter"} <= actions
    # TransactWriteItems is authorized through these item-level actions.
    assert {"dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:ConditionCheckItem"} <= actions
    assert {"bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"} <= actions
    invoke = [s for s in statements if s["Action"] == "bedrock-agentcore:InvokeAgentRuntime"]
    assert len(invoke) == 1
    assert "aldo_placeholder_not_deployed" in json.dumps(invoke[0]["Resource"])
    assert invoke[0]["Resource"] != "*"
    # Both tables are reachable by the worker (it runs the personal agent in process).
    rendered = json.dumps(statements)
    table_ids = {
        t["Properties"]["TableName"]: logical_id
        for logical_id, t in template.find_resources("AWS::DynamoDB::Table").items()
    }
    assert table_ids["aldo-returns-demo"] in rendered
    assert table_ids["aldo-personal-agent-demo"] in rendered


def test_agent_runtime_policy_has_no_access_to_the_personal_agent_table(template: Template) -> None:
    policy = next(
        p
        for p in template.find_resources("AWS::IAM::ManagedPolicy").values()
        if p["Properties"].get("ManagedPolicyName") == "aldo-returns-demo-agent-runtime-extra"
    )
    rendered = json.dumps(policy["Properties"]["PolicyDocument"])
    assert "PersonalAgentTable" not in rendered
    assert "aldo-personal-agent-demo" not in rendered
    assert "dynamodb:" in rendered and "bedrock:InvokeModel" in rendered


def test_only_the_worker_role_can_invoke_the_agent_runtime(template: Template) -> None:
    """Requirement 1.5: exactly one IAM statement in the stack allows bedrock-agentcore actions."""
    holders = []
    for resource_type in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy", "AWS::IAM::Role"):
        for logical_id, resource in template.find_resources(resource_type).items():
            if "bedrock-agentcore:" in json.dumps(resource["Properties"]):
                holders.append(logical_id)
    assert len(holders) == 1 and holders[0].startswith("WorkerFnServiceRoleDefaultPolicy"), holders


def test_runtime_arn_from_context_tightens_the_worker_policy() -> None:
    template = _synth(runtime_arn=RUNTIME_ARN)
    worker = _function(template, "aldo-returns-demo-worker")["Properties"]["Environment"]["Variables"]
    assert worker["AGENT_RUNTIME_ARN"] == RUNTIME_ARN
    statements = _statements(template, "WorkerFnServiceRole")
    (invoke,) = [s for s in statements if s["Action"] == "bedrock-agentcore:InvokeAgentRuntime"]
    assert invoke["Resource"] == [RUNTIME_ARN, f"{RUNTIME_ARN}/runtime-endpoint/*"]


def test_outputs(template: Template) -> None:
    outputs = template.to_json()["Outputs"]
    expected = {
        "CloudFrontUrl",
        "DistributionId",
        "ApiUrl",
        "TableName",
        "PersonalAgentTableName",
        "BucketName",
        "SecretName",
        "ApiFunctionName",
        "WorkerFunctionName",
        "WorkerRoleArn",
        "AgentRuntimeExtraPolicyArn",
        "RuntimeArnParameter",
        "GatewayUrlParameterName",
        "CodeMode",
    }
    assert expected <= set(outputs)


def test_real_code_requires_the_lambda_bundle(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(stack_module, "REAL_CODE_DIR", tmp_path)
    with pytest.raises(Exception, match="bundle_backend.sh --lambda"):
        _synth(use_real_code=True)

    for package in ("api", "worker"):
        (tmp_path / package).mkdir()
        (tmp_path / package / "handler.py").write_text("def handler(event, context):\n    return {}\n")
    template = _synth(use_real_code=True)
    assert template.to_json()["Outputs"]["CodeMode"]["Value"] == "real"
