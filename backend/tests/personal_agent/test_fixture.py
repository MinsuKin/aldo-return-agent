"""fixtures/personal_agent/memory.json: PIVOT_ADDENDUM 5.8 and the size scale of the demo fixtures."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from personal_agent import memory
from returns_domain.errors import DomainError

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = REPO_ROOT / "fixtures" / "personal_agent" / "memory.json"


@pytest.fixture(scope="module")
def data() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def owner(data: dict, owner_id: str) -> dict:
    return next(o for o in data["owners"] if o["owner_id"] == owner_id)


def test_fixture_passes_the_repository_validation() -> None:
    assert memory.load_fixture(FIXTURE)["schema_version"] == 1


def test_ava_matches_the_addendum() -> None:
    ava = owner(json.loads(FIXTURE.read_text(encoding="utf-8")), "DEMO-CUST-AVA")
    assert (ava["display_name"], ava["size_system"], ava["typical_size"], ava["source"]) == (
        "Ava",
        "US_W",
        "7.5",
        "synthetic",
    )
    assert ava["memory"] == [
        {"memory_id": "PM-1", "retailer": "Retailer A (synthetic)", "item": "running shoe", "size": "7.5", "outcome": "kept", "source": "synthetic"},
        {"memory_id": "PM-2", "retailer": "Retailer B (synthetic)", "item": "loafer", "size": "7.5", "outcome": "kept", "source": "synthetic"},
        {"memory_id": "PM-3", "retailer": "Retailer C (synthetic)", "item": "sneaker", "size": "8.0", "outcome": "returned_too_big", "source": "synthetic"},
    ]


def test_ben_and_chloe_have_one_short_memory_each(data: dict) -> None:
    for owner_id in ("DEMO-CUST-BEN", "DEMO-CUST-CHLOE"):
        entries = owner(data, owner_id)["memory"]
        assert len(entries) == 1
        assert entries[0]["outcome"] == "kept"


def test_exactly_the_three_demo_personas(data: dict) -> None:
    assert [o["owner_id"] for o in data["owners"]] == ["DEMO-CUST-AVA", "DEMO-CUST-BEN", "DEMO-CUST-CHLOE"]


def test_sizes_are_strings_with_one_decimal_and_every_record_is_synthetic(data: dict) -> None:
    for entry in data["owners"]:
        assert re.fullmatch(r"\d{1,2}\.\d", entry["typical_size"])
        assert entry["source"] == "synthetic"
        for item in entry["memory"]:
            assert re.fullmatch(r"\d{1,2}\.\d", item["size"])
            assert item["source"] == "synthetic"
            assert item["outcome"] in {"kept", "returned_too_big", "returned_too_small"}


def test_retailer_labels_are_generic_and_never_contain_aldo(data: dict) -> None:
    labels = [m["retailer"] for o in data["owners"] for m in o["memory"]]
    assert labels and all(re.fullmatch(r"Retailer [A-Z] \(synthetic\)", label) for label in labels)
    assert not any("aldo" in label.lower() for label in labels)


def test_the_fixture_never_calls_the_agent_muse(data: dict) -> None:
    assert "muse" not in FIXTURE.read_text(encoding="utf-8").lower()


def test_typical_sizes_sit_inside_the_demo_size_scale(data: dict) -> None:
    ava = owner(data, "DEMO-CUST-AVA")
    assert 6.0 <= float(ava["typical_size"]) <= 9.0  # hero size run 6.0 to 9.0


def test_loader_rejects_a_fixture_that_is_not_synthetic(tmp_path: Path, data: dict) -> None:
    broken = json.loads(json.dumps(data))
    broken["owners"][0]["memory"][0]["source"] = "provided_csv"
    path = tmp_path / "memory.json"
    path.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(DomainError) as caught:
        memory.load_fixture(path)
    assert caught.value.code == "INTERNAL_ERROR"
