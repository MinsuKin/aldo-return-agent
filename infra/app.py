import os

import aws_cdk as cdk

from aldo_returns_demo.stack import AldoReturnsDemoStack

app = cdk.App()


def _flag(name: str) -> bool:
    return str(app.node.try_get_context(name)).strip().lower() in ("1", "true", "yes")


AldoReturnsDemoStack(
    app,
    "AldoReturnsDemo",
    # Region is fixed. The account comes from the credentials the CLI runs with.
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region="us-east-1"),
    # cdk deploy -c useRealCode=true switches the Lambda assets from infra/stubs to
    # infra/build/lambda (built by scripts/bundle_backend.sh --lambda).
    use_real_code=_flag("useRealCode"),
    # cdk deploy -c runtimeArn=arn:aws:bedrock-agentcore:... pins the worker IAM policy to
    # that exact runtime and sets AGENT_RUNTIME_ARN. Unset: the policy covers runtimes named
    # <agentRuntimeName>-* and the worker reads the SSM parameter /aldo-demo/runtime-arn.
    runtime_arn=app.node.try_get_context("runtimeArn"),
    agent_runtime_name=app.node.try_get_context("agentRuntimeName") or "aldo_returns_agent",
)
app.synth()
