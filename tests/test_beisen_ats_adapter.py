import json
from pathlib import Path

import pytest

from src.job_collection.adapters.base import AdapterRecordError, AdapterStructureError
from src.job_collection.adapters.beisen_ats import BeisenATSAdapter
from src.job_collection.models import SourceDefinition
from src.job_collection.source_registry import SourceRegistry


FIXTURE = Path(__file__).parent / "fixtures" / "company_ats" / "beisen-list.json"


def make_source() -> SourceDefinition:
    return SourceDefinition.model_validate(
        {
            "source_id": "company_beisen_dreame",
            "source_name": "追觅科技官方招聘",
            "source_type": "company_official",
            "market_scope": "china",
            "base_url": "https://dreame.zhiye.com",
            "allowed_paths": ["/api/Jobad/GetJobAdPageList", "/social/jobs"],
            "collection_mode": "public_json",
            "compliance_status": "approved",
            "compliance_note": "人工复核日期 2026-08-13：公开企业招聘门户。",
            "rate_limit_seconds": 3.0,
            "max_pages": 10,
            "max_records": 300,
            "parser_name": "beisen_company_ats",
            "parser_version": "v1",
            "organization_name": "追觅科技",
            "enabled": True,
        }
    )


def make_adapter() -> BeisenATSAdapter:
    source = make_source()
    return BeisenATSAdapter(source=source, registry=SourceRegistry([source]))


def test_build_list_request_uses_public_social_recruitment_contract():
    request = make_adapter().build_list_request("Python", 20, 20)

    assert request.method == "POST"
    assert request.url == "https://dreame.zhiye.com/api/Jobad/GetJobAdPageList"
    assert request.headers == {
        "Origin": "https://dreame.zhiye.com",
        "Referer": "https://dreame.zhiye.com/social/jobs",
    }
    assert request.json_body == {
        "PageIndex": 1,
        "PageSize": 20,
        "LocId": [],
        "Category": ["1"],
        "KeyWords": "Python",
        "SpecialType": 0,
        "PortalId": "",
        "DisplayFields": ["Category", "Kind", "LocId", "PostDate", "Salary"],
    }


def test_parse_list_and_embedded_detail_preserve_traceable_fields():
    adapter = make_adapter()
    page = adapter.parse_list(
        FIXTURE.read_bytes(),
        "application/json",
        expected_offset=0,
        expected_limit=20,
    )

    assert page.total == 2
    assert page.has_more is False
    assert page.items[1].region == "江苏省·苏州市, 上海市"
    assert page.items[1].salary == "20-35K/月"

    item = page.items[0]
    url = adapter.build_detail_url(item)
    detail = adapter.parse_detail(b"", item, url)

    assert url == "https://dreame.zhiye.com/social/jobs"
    assert detail["company_name"] == "追觅科技"
    assert detail["published_at"] == "2026-07-06T09:03:29"
    assert detail["salary"] == "薪资面议"
    assert detail["adapter_extra"] == {
        "ats_type": "beisen",
        "recruitment_type": "社会招聘",
        "request_number": "J14096",
    }


@pytest.mark.parametrize(
    "document",
    [
        [],
        {},
        {"Data": {}, "Count": 0},
        {"Data": [], "Count": -1},
    ],
)
def test_parse_list_rejects_unreviewed_response_shapes(document):
    with pytest.raises(AdapterStructureError):
        make_adapter().parse_list(
            json.dumps(document).encode(),
            "application/json",
            expected_offset=0,
            expected_limit=20,
        )


def test_parse_detail_rejects_empty_duty():
    adapter = make_adapter()
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["Data"][0]["Duty"] = ""
    page = adapter.parse_list(
        json.dumps(document).encode(),
        "application/json",
        expected_offset=0,
        expected_limit=20,
    )

    with pytest.raises(AdapterRecordError, match="description"):
        adapter.parse_detail(b"", page.items[0], adapter.build_detail_url(page.items[0]))
