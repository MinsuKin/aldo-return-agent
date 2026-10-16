"""The data-platform two-way door (returns_domain.data_source): static by default, platform and shadow on request, never worse than static."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Iterator

import boto3
import pytest
from moto import mock_aws

from returns_domain import data_source as ds

ROOT = Path(__file__).resolve().parents[2]
BUCKET = "aldo-data-platform-test"
PREFIX = "curated/fit_insights/"
KEY = f"{PREFIX}v=2026-10-15T23-10-00Z/fit_insights.json"


def _entry(small: int, big: int, total: int) -> dict[str, Any]:
    return {"too_small": small, "too_big": big, "total_returns": total}


def _doc(*, big: int = 109, platform: bool = True, products: dict[str, Any] | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "generated_at": "2026-10-15T23:10:00+00:00",
        "provenance": "derived",
        "source": "aws-athena" if platform else {"kind": "csv"},
        "thresholds": {"min_n": 30},
        "totals": {"ALDO_CA": {"return_lines": 10}, "ALL": {"return_lines": 20}},
        "products": products
        if products is not None
        else {"Toney": {"ALDO_CA": _entry(9, big, 150), "ALL": _entry(12, 140, 400)}, "Oscar": {"ALDO_CA": _entry(5, 6, 30), "ALL": _entry(7, 9, 60)}},
    }
    if platform:
        doc["platform"] = {"engine": "athena", "workgroup": "aldo-demo", "query_execution_ids": ["q-1", "q-2"], "pipeline_version": 1}
    return doc


BUNDLED = _doc(platform=False)


def _bundled() -> dict[str, Any]:
    return BUNDLED


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in ("ALDO_DATA_MODE", "ALDO_DATA_SSM", "AWS_LAMBDA_FUNCTION_NAME", "AWS_PROFILE", "AWS_ENDPOINT_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    ds.reset_for_tests()
    yield
    ds.reset_for_tests()


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """moto S3 + SSM with the platform fully configured (mode `platform`), AWS access enabled."""
    monkeypatch.setenv("ALDO_DATA_SSM", "1")
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        ssm = boto3.client("ssm", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        s3.put_object(Bucket=BUCKET, Key=KEY, Body=json.dumps(_doc()).encode())
        s3.put_object(Bucket=BUCKET, Key=f"{PREFIX}latest.json", Body=json.dumps({"key": KEY, "etag": "e1", "generated_at": "x"}).encode())
        ssm.put_parameter(Name=ds.PARAM_CONFIG, Value=json.dumps({"bucket": BUCKET, "curated_prefix": "curated/"}), Type="String")  # exactly what the stack writes
        ssm.put_parameter(Name=ds.PARAM_MODE, Value="platform", Type="String")
        yield type("Aws", (), {"s3": s3, "ssm": ssm})


def test_static_is_the_default_and_makes_no_aws_call() -> None:
    assert ds.current_mode() == "static"
    assert ds.resolve_insights(_bundled) is BUNDLED
    assert ds._clients == {}  # no boto3 client was even created


def test_static_stays_static_when_aws_is_not_enabled_even_if_a_mode_parameter_exists(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALDO_DATA_SSM")
    assert ds.current_mode() == "static"
    assert ds.resolve_insights(_bundled) is BUNDLED


def test_the_env_override_wins_over_ssm(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALDO_DATA_MODE", "static")
    assert ds.current_mode() == "static"
    assert ds.resolve_insights(_bundled) is BUNDLED


def test_platform_serves_the_curated_document_and_names_its_source(aws: Any) -> None:
    doc = ds.resolve_insights(_bundled)
    assert doc is not BUNDLED
    assert doc["products"]["Toney"]["ALDO_CA"]["too_big"] == 109
    info = ds.source_info(doc)
    assert info == {"data_source": "aws-athena", "query_execution_ids": ["q-1", "q-2"], "generated_at": "2026-10-15T23:10:00+00:00"}
    assert ds.source_info(BUNDLED) == {"data_source": "bundled-json"}


@pytest.mark.parametrize("breakage", ["no_config", "no_pointer", "no_object", "bad_json", "malformed", "bad_pointer"])
def test_every_platform_failure_falls_back_to_the_bundled_file(aws: Any, breakage: str, caplog: pytest.LogCaptureFixture) -> None:
    if breakage == "no_config":
        aws.ssm.delete_parameter(Name=ds.PARAM_CONFIG)
    elif breakage == "no_pointer":
        aws.s3.delete_object(Bucket=BUCKET, Key=f"{PREFIX}latest.json")
    elif breakage == "no_object":
        aws.s3.delete_object(Bucket=BUCKET, Key=KEY)
    elif breakage == "bad_json":
        aws.s3.put_object(Bucket=BUCKET, Key=KEY, Body=b"{not json")
    elif breakage == "malformed":
        aws.s3.put_object(Bucket=BUCKET, Key=KEY, Body=json.dumps({"products": {}}).encode())
    elif breakage == "bad_pointer":
        aws.s3.put_object(Bucket=BUCKET, Key=f"{PREFIX}latest.json", Body=json.dumps({"nothing": 1}).encode())
    with caplog.at_level(logging.WARNING, logger=ds.log.name):
        assert ds.resolve_insights(_bundled) is BUNDLED
    assert "data_platform_fallback" in caplog.text
    assert ds.status()["available"] is False


def test_an_unknown_mode_value_in_ssm_means_static(aws: Any) -> None:
    aws.ssm.put_parameter(Name=ds.PARAM_MODE, Value="turbo", Type="String", Overwrite=True)
    assert ds.current_mode() == "static"


def test_shadow_serves_the_bundled_file_and_reports_how_the_documents_differ(aws: Any, caplog: pytest.LogCaptureFixture) -> None:
    aws.ssm.put_parameter(Name=ds.PARAM_MODE, Value="shadow", Type="String", Overwrite=True)
    bundled = _doc(platform=False, big=100)  # the curated document says 109 for Toney
    with caplog.at_level(logging.INFO, logger=ds.log.name):
        assert ds.resolve_insights(lambda: bundled) is bundled
        assert ds.resolve_insights(lambda: bundled) is bundled  # the comparison is logged once per document
    assert caplog.text.count("data_platform_shadow_mismatch") == 1
    result = ds.status()["shadow"][KEY]
    assert result["mismatched"] == 1 and result["examples"] == ["Toney"]
    assert result["products_platform"] == 2 and result["products_bundled"] == 2


def test_shadow_reports_ok_when_the_counts_are_equal(aws: Any, caplog: pytest.LogCaptureFixture) -> None:
    aws.ssm.put_parameter(Name=ds.PARAM_MODE, Value="shadow", Type="String", Overwrite=True)
    same = _doc()  # the curated document equal to the bundled one, count for count
    aws.s3.put_object(Bucket=BUCKET, Key=KEY, Body=json.dumps(same).encode())
    ds.reset_for_tests()
    with caplog.at_level(logging.INFO, logger=ds.log.name):
        ds.resolve_insights(lambda: same)
    assert "data_platform_shadow_ok" in caplog.text
    assert ds.status()["shadow"][KEY]["mismatched"] == 0


def test_compare_documents_counts_mismatches_and_one_sided_products() -> None:
    a = _doc(big=110)
    b = _doc(products={"Toney": {"ALDO_CA": _entry(9, 109, 150), "ALL": _entry(12, 140, 400)}, "Extra": {"ALDO_CA": _entry(1, 1, 2), "ALL": _entry(1, 1, 2)}})
    result = ds.compare_documents(a, b)
    assert result["mismatched"] == 1 and result["examples"] == ["Toney"]
    assert result["only_bundled"] == 1 and result["only_platform"] == 1


def test_the_mode_is_cached_for_15_seconds_then_read_again(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(ds, "_clock", lambda: now[0])
    assert ds.current_mode() == "platform"
    aws.ssm.put_parameter(Name=ds.PARAM_MODE, Value="static", Type="String", Overwrite=True)
    now[0] += 5
    assert ds.current_mode() == "platform"  # still cached
    now[0] += 11
    assert ds.current_mode() == "static"


def test_a_new_curated_version_is_picked_up_after_the_pointer_cache_expires(aws: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(ds, "_clock", lambda: now[0])
    assert ds.resolve_insights(_bundled)["products"]["Toney"]["ALDO_CA"]["too_big"] == 109
    key2 = f"{PREFIX}v=2026-10-16T01-00-00Z/fit_insights.json"
    aws.s3.put_object(Bucket=BUCKET, Key=key2, Body=json.dumps(_doc(big=111)).encode())
    aws.s3.put_object(Bucket=BUCKET, Key=f"{PREFIX}latest.json", Body=json.dumps({"key": key2, "etag": "e2", "generated_at": "y"}).encode())
    now[0] += 30
    assert ds.resolve_insights(_bundled)["products"]["Toney"]["ALDO_CA"]["too_big"] == 109  # pointer still cached
    now[0] += 40
    assert ds.resolve_insights(_bundled)["products"]["Toney"]["ALDO_CA"]["too_big"] == 111


def test_set_mode_writes_the_parameter_and_rejects_unknown_modes(aws: Any) -> None:
    out = ds.set_mode("shadow")
    assert out["mode"] == "shadow"
    assert aws.ssm.get_parameter(Name=ds.PARAM_MODE)["Parameter"]["Value"] == "shadow"
    with pytest.raises(ValueError):
        ds.set_mode("turbo")
    assert aws.ssm.get_parameter(Name=ds.PARAM_MODE)["Parameter"]["Value"] == "shadow"


def test_status_reports_the_curated_document_when_it_is_available(aws: Any) -> None:
    out = ds.status()
    assert out["mode"] == "platform" and out["available"] is True
    assert out["curated"]["key"] == KEY and out["curated"]["products"] == 2
    assert out["curated"]["query_execution_ids"] == ["q-1", "q-2"]


def test_status_in_static_without_aws_is_quiet() -> None:
    assert ds.status() == {"mode": "static", "modes": ["static", "platform", "shadow"], "aws_enabled": False, "available": False}


# ---- where the pointer lives: the stack, the pipeline and the loader must agree (a mismatch would hide behind the static fall-back)


def _constant(path: str, name: str) -> str:
    text = (ROOT / path).read_text(encoding="utf-8")
    found = re.search(rf'^{name}\s*=\s*"([^"]*)"', text, re.M)
    assert found, f"{name} not found in {path}"
    return found.group(1)


def test_stack_pipeline_and_loader_agree_on_where_the_pointer_lives() -> None:
    stack_prefix = _constant("infra/aldo_returns_demo/data_platform_stack.py", "CURATED_PREFIX")
    pipeline_dir = _constant("scripts/data_platform/build_insights.py", "CURATED_FIT_PREFIX")
    assert ds.DEFAULT_PREFIX == stack_prefix
    assert ds.pointer_key(stack_prefix) == f"{pipeline_dir}/latest.json"


@pytest.mark.parametrize("prefix", ["curated/", "curated", "curated/fit_insights/", "curated/fit_insights", " curated/ ", None, ""])
def test_pointer_key_accepts_every_spelling_of_the_prefix(prefix: str | None) -> None:
    assert ds.pointer_key(prefix) == "curated/fit_insights/latest.json"


def test_a_config_without_a_prefix_still_finds_the_curated_document(aws: Any) -> None:
    aws.ssm.put_parameter(Name=ds.PARAM_CONFIG, Value=json.dumps({"bucket": BUCKET}), Type="String", Overwrite=True)
    ds.reset_for_tests()
    assert ds.current_mode() == "platform"
    assert ds.status()["available"] is True
