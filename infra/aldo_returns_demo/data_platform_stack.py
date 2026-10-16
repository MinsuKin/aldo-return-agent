"""AldoDataPlatform: the removable data-platform unit (design docs/data-platform/DESIGN.md).

A separate CDK stack so the whole thing is a two-way door: deleting it returns the
account to today's state. It adds ONLY new resources and never touches the main
AldoReturnsDemo stack or the AgentCore runtime.

What it creates (design "Names and layout"):
  * S3 bucket  aldo-data-platform-<account>-us-east-1
      BlockPublicAccess ALL, SSE-S3, enforceSSL, removal DESTROY + autoDeleteObjects
      (so the owner can `cdk destroy` cleanly; agents never delete).
  * Glue Data Catalog database  aldo_oms
  * Athena workgroup  aldo-demo  (engine v3, enforced result location
      s3://<bucket>/athena-results/, 10 GB per-query scan cutoff)
  * SSM  /aldo-demo/data-platform/config  (JSON: bucket, region, database,
      workgroup, curated_prefix) and  /aldo-demo/data-platform/mode  (= "static")
  * ONE managed policy  AldoDataPlatformAccess  attached to the EXISTING worker and
      API Lambda roles (imported read-only by ARN, mutable=True):
        - s3:GetObject / s3:ListBucket scoped to curated/*
        - ssm:GetParameter on /aldo-demo/data-platform/*
        - ssm:PutParameter on the mode parameter only
      Athena/Glue permissions are NOT granted here (they belong to the pipeline's
      own credentials, not to the request-path Lambdas).

The stack is only instantiated by infra/app.py when `-c dataPlatform=true`; without
the flag the synthesized app is byte-for-byte unchanged. The worker/API role ARNs are
passed as CDK context (found read-only from outputs.json / describe-stacks /
get-function-configuration) so this stack creates no new roles.
"""
from __future__ import annotations

import json

from aws_cdk import (
    CfnOutput,
    RemovalPolicy,
    Stack,
    Tags,
    aws_athena as athena,
    aws_glue as glue,
    aws_iam as iam,
    aws_s3 as s3,
    aws_ssm as ssm,
)
from constructs import Construct

REGION = "us-east-1"
BUCKET_PREFIX = "aldo-data-platform"
GLUE_DATABASE = "aldo_oms"
WORKGROUP = "aldo-demo"
ATHENA_RESULTS_PREFIX = "athena-results/"
CURATED_PREFIX = "curated/"
SCAN_CUTOFF_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB

CONFIG_PARAM = "/aldo-demo/data-platform/config"
MODE_PARAM = "/aldo-demo/data-platform/mode"
DATA_PLATFORM_PARAM_PREFIX = "/aldo-demo/data-platform/"

MANAGED_POLICY_NAME = "AldoDataPlatformAccess"


class AldoDataPlatformStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        worker_role_arn: str | None,
        api_role_arn: str | None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        Tags.of(self).add("project", "aldo-returns-demo")
        Tags.of(self).add("component", "data-platform")

        account = self.account
        bucket_name = f"{BUCKET_PREFIX}-{account}-{REGION}"

        # --- Private, encrypted, TLS-only bucket. DESTROY + autoDelete so the owner
        #     can tear the whole stack down with one `cdk destroy`.
        bucket = s3.Bucket(
            self,
            "DataPlatformBucket",
            bucket_name=bucket_name,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            versioned=False,
        )

        # --- Glue Data Catalog database aldo_oms (external-table catalog only).
        glue.CfnDatabase(
            self,
            "OmsDatabase",
            catalog_id=account,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name=GLUE_DATABASE,
                description="ALDO OMS external tables for the data-platform demo",
            ),
        )

        # --- Athena workgroup aldo-demo: engine v3, enforced result location, 10 GB cutoff.
        athena.CfnWorkGroup(
            self,
            "DemoWorkGroup",
            name=WORKGROUP,
            description="ALDO data-platform demo workgroup (engine v3, enforced results, 10 GB cutoff)",
            recursive_delete_option=True,
            state="ENABLED",
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                enforce_work_group_configuration=True,
                publish_cloud_watch_metrics_enabled=False,
                engine_version=athena.CfnWorkGroup.EngineVersionProperty(
                    selected_engine_version="Athena engine version 3",
                ),
                bytes_scanned_cutoff_per_query=SCAN_CUTOFF_BYTES,
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{bucket_name}/{ATHENA_RESULTS_PREFIX}",
                    encryption_configuration=athena.CfnWorkGroup.EncryptionConfigurationProperty(
                        encryption_option="SSE_S3",
                    ),
                ),
            ),
        )

        # --- SSM: config JSON + mode (created as "static", the two-way-door default).
        config_value = json.dumps(
            {
                "bucket": bucket_name,
                "region": REGION,
                "database": GLUE_DATABASE,
                "workgroup": WORKGROUP,
                "curated_prefix": CURATED_PREFIX,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        ssm.StringParameter(
            self,
            "ConfigParam",
            parameter_name=CONFIG_PARAM,
            string_value=config_value,
            description="ALDO data-platform config (bucket, region, database, workgroup, curated_prefix)",
        )
        ssm.StringParameter(
            self,
            "ModeParam",
            parameter_name=MODE_PARAM,
            string_value="static",
            description="ALDO data-platform mode: static | platform | shadow (created static)",
        )

        # --- One managed policy, attached to the EXISTING Lambda roles (imported by ARN).
        mode_param_arn = self.format_arn(
            service="ssm",
            resource="parameter",
            resource_name=MODE_PARAM.lstrip("/"),
        )
        data_platform_param_arn = self.format_arn(
            service="ssm",
            resource="parameter",
            resource_name=DATA_PLATFORM_PARAM_PREFIX.lstrip("/") + "*",
        )
        access_policy = iam.ManagedPolicy(
            self,
            "DataPlatformAccess",
            managed_policy_name=MANAGED_POLICY_NAME,
            description="Request-path read access to the ALDO data platform (curated/* + SSM mode flip)",
            statements=[
                iam.PolicyStatement(
                    sid="ReadCuratedObjects",
                    effect=iam.Effect.ALLOW,
                    actions=["s3:GetObject"],
                    resources=[bucket.arn_for_objects(f"{CURATED_PREFIX}*")],
                ),
                iam.PolicyStatement(
                    sid="ListCuratedPrefix",
                    effect=iam.Effect.ALLOW,
                    actions=["s3:ListBucket"],
                    resources=[bucket.bucket_arn],
                    conditions={"StringLike": {"s3:prefix": [f"{CURATED_PREFIX}*"]}},
                ),
                iam.PolicyStatement(
                    sid="ReadDataPlatformParams",
                    effect=iam.Effect.ALLOW,
                    actions=["ssm:GetParameter"],
                    resources=[data_platform_param_arn],
                ),
                iam.PolicyStatement(
                    sid="WriteModeParamOnly",
                    effect=iam.Effect.ALLOW,
                    actions=["ssm:PutParameter"],
                    resources=[mode_param_arn],
                ),
            ],
        )

        attached = []
        for label, arn in (("WorkerRole", worker_role_arn), ("ApiRole", api_role_arn)):
            if not arn:
                continue
            role = iam.Role.from_role_arn(self, f"Imported{label}", arn, mutable=True)
            access_policy.attach_to_role(role)
            attached.append(label)

        CfnOutput(self, "DataPlatformBucketName", value=bucket_name)
        CfnOutput(self, "GlueDatabaseName", value=GLUE_DATABASE)
        CfnOutput(self, "AthenaWorkGroupName", value=WORKGROUP)
        CfnOutput(self, "ConfigParameterName", value=CONFIG_PARAM)
        CfnOutput(self, "ModeParameterName", value=MODE_PARAM)
        CfnOutput(self, "AccessPolicyName", value=MANAGED_POLICY_NAME)
        CfnOutput(self, "AccessPolicyAttachedTo", value=",".join(attached) or "none")
