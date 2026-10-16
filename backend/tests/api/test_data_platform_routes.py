"""Admin routes of the data-platform door: GET/POST /api/demo/data-platform."""

from __future__ import annotations

from typing import Iterator

import boto3
import pytest

from returns_domain import audit
from returns_domain import data_source as ds

from .conftest import ADMIN_KEY

ADMIN = {"X-Demo-Admin": ADMIN_KEY}


@pytest.fixture(autouse=True)
def _door(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in ("ALDO_DATA_MODE", "ALDO_DATA_SSM"):
        monkeypatch.delenv(name, raising=False)
    ds.reset_for_tests()
    yield
    ds.reset_for_tests()


def test_both_routes_need_the_admin_key(client):
    assert client.get("/api/demo/data-platform").status in (401, 403)
    assert client.post("/api/demo/data-platform", {"mode": "static"}).status in (401, 403)
    assert client.get("/api/demo/data-platform", headers={"X-Demo-Admin": "wrong"}).status in (401, 403)


def test_status_in_a_plain_test_run_says_static_and_unavailable(client):
    reply = client.get("/api/demo/data-platform", headers=ADMIN)
    assert reply.status == 200
    assert reply.json["mode"] == "static" and reply.json["available"] is False and reply.json["modes"] == ["static", "platform", "shadow"]


def test_the_mode_can_be_flipped_and_flipped_back(client, monkeypatch):
    monkeypatch.setenv("ALDO_DATA_SSM", "1")
    ssm = boto3.client("ssm", region_name="us-east-1")
    ssm.put_parameter(Name=ds.PARAM_MODE, Value="static", Type="String")
    flipped = client.post("/api/demo/data-platform", {"mode": "shadow"}, headers=ADMIN)
    assert flipped.status == 200 and flipped.json["mode"] == "shadow"
    assert ssm.get_parameter(Name=ds.PARAM_MODE)["Parameter"]["Value"] == "shadow"
    assert client.get("/api/demo/data-platform", headers=ADMIN).json["mode"] == "shadow"
    back = client.post("/api/demo/data-platform", {"mode": "static"}, headers=ADMIN)
    assert back.json["mode"] == "static"
    assert ssm.get_parameter(Name=ds.PARAM_MODE)["Parameter"]["Value"] == "static"


@pytest.mark.parametrize("body", [{"mode": "turbo"}, {}, {"mode": 5}])
def test_an_unknown_mode_is_a_validation_error_and_changes_nothing(client, monkeypatch, body):
    monkeypatch.setenv("ALDO_DATA_SSM", "1")
    ssm = boto3.client("ssm", region_name="us-east-1")
    ssm.put_parameter(Name=ds.PARAM_MODE, Value="static", Type="String")
    reply = client.post("/api/demo/data-platform", body, headers=ADMIN)
    assert reply.status == 422
    assert ssm.get_parameter(Name=ds.PARAM_MODE)["Parameter"]["Value"] == "static"


def test_the_flip_is_audited(client, seeded, monkeypatch):
    monkeypatch.setenv("ALDO_DATA_SSM", "1")
    boto3.client("ssm", region_name="us-east-1").put_parameter(Name=ds.PARAM_MODE, Value="static", Type="String")
    assert client.post("/api/demo/data-platform", {"mode": "platform"}, headers=ADMIN).status == 200
    rows = audit.list_audit(action="data_platform_mode")
    assert len(rows) == 1 and rows[0]["actor"] == "admin" and rows[0]["outcome"] == "allowed"
