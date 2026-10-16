"""The data-platform pipeline, without AWS: the shaping step is the inverse of the bundled builder, and the SQL files hang together.

`shape_document` turns Athena result rows into the curated document. Feeding it the rows that the bundled document implies must give
the bundled document back (full parity), and the door must accept what comes out. The live parity run against real Athena then only has
to prove the SQL itself, not the Python around it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from typing import Any

import pytest

from returns_domain import data_source as ds

ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "scripts" / "data_platform"
BUNDLED = ROOT / "fixtures" / "derived" / "fit_insights.json"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"data_platform_{name}", PIPELINE / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def build() -> ModuleType:
    return _load("build_insights")


@pytest.fixture(scope="module")
def parity() -> ModuleType:
    return _load("parity_report")


def _bundled() -> dict[str, Any]:
    return json.loads(BUNDLED.read_text(encoding="utf-8"))


def _rows_implied_by(doc: dict[str, Any]) -> list[list[str | None]]:
    """The rows the fit query returns for this document: a product row only where the scope has lines, and one total row per scope."""
    rows: list[list[str | None]] = []
    for name, scopes in doc["products"].items():
        if "alias_of" in scopes:  # a demo alias, not a product of the data: the pipeline adds those itself
            continue
        for scope in ("ALDO_CA", "ALL"):
            entry = scopes[scope]
            if int(entry["total_returns"]) > 0:
                rows.append(["product", scope, name, str(entry["total_returns"]), str(entry["too_small"]), str(entry["too_big"]), "0", "0"])
    for scope, total in doc["totals"].items():
        rows.append(["total", scope, None, str(total["return_lines"]), str(total["too_small"]), str(total["too_big"]),
                     str(total["lines_without_product_name"]), str(total["products"])])
    return rows


def _shaped(build: ModuleType) -> dict[str, Any]:
    athena = SimpleNamespace(exec_ids=["q-1", "q-2"], bytes_scanned=123)
    aliases = build.add_fit_aliases.load_mapping(build.add_fit_aliases.DEFAULT_ALIASES)  # what main() passes
    return build.shape_document(_rows_implied_by(_bundled()), {"files": {"orders": "sha-a", "returns": "sha-b"}}, athena, aliases)


def test_round_trip_of_the_bundled_document_has_full_parity(build: ModuleType, parity: ModuleType) -> None:
    report = parity.compare(_bundled(), _shaped(build))
    assert report["product_mismatches"] == []
    assert report["total_mismatches"] == []
    assert report["products_in_curated"] == report["products_in_bundled"] > 100


def test_demo_aliases_resolve_in_the_curated_document_like_in_the_bundled_one(build: ModuleType) -> None:
    bundled, shaped = _bundled(), _shaped(build)
    aliases = {name for name, entry in bundled["products"].items() if "alias_of" in entry}
    assert aliases, "the bundled file is expected to carry the demo alias entries"
    for name in aliases:
        assert shaped["products"][name]["alias_of"] == bundled["products"][name]["alias_of"]
        assert shaped["products"][name]["ALDO_CA"] == bundled["products"][name]["ALDO_CA"]
    assert shaped["platform"]["aliases_applied"] == len(aliases)


def test_shaped_document_gives_the_same_answers_per_product_and_scope(build: ModuleType) -> None:
    bundled, shaped = _bundled(), _shaped(build)
    for name, scopes in bundled["products"].items():
        for scope in ("ALDO_CA", "ALL"):
            entry = scopes[scope]
            got = shaped["products"][name][scope]
            for key in ("too_small", "too_big", "total_returns", "n_fit", "signal", "too_small_share"):
                assert got[key] == entry[key], (name, scope, key)


def test_shaped_document_is_accepted_by_the_door_and_names_its_queries(build: ModuleType) -> None:
    shaped = _shaped(build)
    assert ds._valid(shaped)
    assert shaped["platform"]["query_execution_ids"] == ["q-1", "q-2"]
    assert shaped["source"]["kind"] == "aws-athena"
    assert shaped["thresholds"]["min_n"] == _bundled()["thresholds"]["min_n"]
    # the same top-level shape as the bundled file, plus the platform block and nothing else
    assert set(shaped) - set(_bundled()) == {"platform"}
    assert set(_bundled()) - set(shaped) == set()


def test_a_wrong_count_is_reported_as_a_mismatch(build: ModuleType, parity: ModuleType) -> None:
    rows = _rows_implied_by(_bundled())
    first_product = next(r for r in rows if r[0] == "product")
    first_product[5] = str(int(first_product[5]) + 1)  # one more 'too big' than the bundled file says
    aliases = build.add_fit_aliases.load_mapping(build.add_fit_aliases.DEFAULT_ALIASES)
    shaped = build.shape_document(rows, {}, SimpleNamespace(exec_ids=[], bytes_scanned=0), aliases)
    report = parity.compare(_bundled(), shaped)
    assert {m["key"] for m in report["product_mismatches"]} == {"too_big"}  # the product itself (and its alias, when it is the aliased style)


def test_sql_files_are_complete_after_substitution_and_hang_together(build: ModuleType) -> None:
    statements: dict[str, list[str]] = {}
    for name in ("01_create_raw_tables.sql", "02_ctas_parquet.sql", "03_fit_insights.sql"):
        text = build.load_sql(name, "aldo_oms", "bucket-x")
        assert "${" not in text, f"{name} has an unresolved placeholder"
        statements[name] = build.split_statements(text)
        assert statements[name], f"{name} has no statement"
        assert not any(line.strip().startswith("--") for stmt in statements[name] for line in stmt.splitlines()), f"{name}: comment lines reach Athena"
    assert len(statements["03_fit_insights.sql"]) == 1  # one query, run once
    fit_query = statements["03_fit_insights.sql"][0]
    assert fit_query.lstrip().upper().startswith("WITH")
    # the query reads a table the CTAS step creates, in the database the stack creates
    assert "aldo_oms.curated_returns" in fit_query
    assert "curated_returns" in "\n".join(statements["02_ctas_parquet.sql"])
    # the raw tables read what the uploader writes
    raw = "\n".join(statements["01_create_raw_tables.sql"])
    assert "s3://bucket-x/raw/" in raw


def test_curated_prefix_in_the_pipeline_matches_where_the_uploader_and_door_look(build: ModuleType) -> None:
    assert build.CURATED_FIT_PREFIX == "curated/fit_insights"
    assert ds.pointer_key("curated/") == f"{build.CURATED_FIT_PREFIX}/latest.json"
