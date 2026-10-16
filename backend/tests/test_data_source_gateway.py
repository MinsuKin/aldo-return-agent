"""The data-platform door through the real gateway (moto DynamoDB + S3 + SSM): the fit_signal_shared event names its source
in platform mode only, the static event is unchanged, and a broken platform falls back to the bundled numbers."""

from __future__ import annotations

import copy
import json
from typing import Any, Iterator

import boto3
import pytest

from returns_domain import data_source as ds
from returns_domain import fit

from .test_gateway_ddb import HERO, a2a, assist, product_name_of

BUCKET = "aldo-data-platform-test"
PREFIX = "curated/fit_insights/"
KEY = f"{PREFIX}v=2026-10-15T23-10-00Z/fit_insights.json"


@pytest.fixture(autouse=True)
def _door(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in ("ALDO_DATA_MODE", "ALDO_DATA_SSM"):
        monkeypatch.delenv(name, raising=False)
    ds.reset_for_tests()
    fit.clear_cache()
    yield
    ds.reset_for_tests()
    fit.clear_cache()


def _curated(extra_small: int) -> dict[str, Any]:
    """The bundled document with the hero style's ALDO_CA `too_small` raised, plus the platform block."""
    doc = copy.deepcopy(fit.load_insights())
    name = product_name_of(HERO)
    doc["products"][name]["ALDO_CA"]["too_small"] += extra_small
    doc["source"] = "aws-athena"
    doc["platform"] = {"engine": "athena", "workgroup": "aldo-demo", "query_execution_ids": ["q-1", "q-2"], "pipeline_version": 1}
    return doc


def _publish(doc: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALDO_DATA_SSM", "1")
    s3 = boto3.client("s3", region_name="us-east-1")
    ssm = boto3.client("ssm", region_name="us-east-1")
    s3.create_bucket(Bucket=BUCKET)
    s3.put_object(Bucket=BUCKET, Key=KEY, Body=json.dumps(doc).encode())
    s3.put_object(Bucket=BUCKET, Key=f"{PREFIX}latest.json", Body=json.dumps({"key": KEY, "etag": "e1"}).encode())
    ssm.put_parameter(Name=ds.PARAM_CONFIG, Value=json.dumps({"bucket": BUCKET, "curated_prefix": PREFIX}), Type="String")


def _shared_event() -> dict[str, Any]:
    return next(e for e in reversed(a2a()) if e["type"] == "fit_signal_shared")["payload"]


def test_static_mode_event_is_unchanged(seeded, fixed_now):
    response = assist()
    payload = _shared_event()
    assert "data_source" not in payload and "query_execution_ids" not in payload
    assert payload["n_fit"] == response.fit_signal.n_fit


def test_platform_mode_serves_the_curated_numbers_and_names_the_source(seeded, fixed_now, monkeypatch):
    static_n_fit = assist().fit_signal.n_fit
    _publish(_curated(extra_small=1000), monkeypatch)
    monkeypatch.setenv("ALDO_DATA_MODE", "platform")
    response = assist()
    assert response.fit_signal.n_fit == static_n_fit + 1000  # the numbers came from the curated document
    payload = _shared_event()
    assert payload["data_source"] == "aws-athena"
    assert payload["query_execution_ids"] == ["q-1", "q-2"]
    assert payload["n_fit"] == response.fit_signal.n_fit


def test_a_broken_platform_falls_back_to_the_bundled_numbers_without_an_error(seeded, fixed_now, monkeypatch):
    static_n_fit = assist().fit_signal.n_fit
    _publish(_curated(extra_small=1000), monkeypatch)
    boto3.client("s3", region_name="us-east-1").put_object(Bucket=BUCKET, Key=KEY, Body=b"not json")
    monkeypatch.setenv("ALDO_DATA_MODE", "platform")
    response = assist()
    assert response.fit_signal.n_fit == static_n_fit
    assert "data_source" not in _shared_event()


def test_shadow_mode_serves_the_bundled_numbers(seeded, fixed_now, monkeypatch):
    static_n_fit = assist().fit_signal.n_fit
    _publish(_curated(extra_small=1000), monkeypatch)
    monkeypatch.setenv("ALDO_DATA_MODE", "shadow")
    response = assist()
    assert response.fit_signal.n_fit == static_n_fit
    assert "data_source" not in _shared_event()
    assert ds.status()["shadow"][KEY]["mismatched"] == 1
