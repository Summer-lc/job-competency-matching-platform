import json
from pathlib import Path

import pytest

from src.job_collection.adapters.base import AdapterRecordError, AdapterStructureError
from src.job_collection.adapters.feishu_ats import FeishuATSAdapter
from src.job_collection.models import SourceDefinition
from src.job_collection.source_registry import SourceRegistry


FIXTURE = Path(__file__).parent / "fixtures" / "company_ats" / "feishu-list.json"


def make_source() -> SourceDefinition:
    return SourceDefinition.model_validate(
        {
            "source_id": "company_feishu_zhipu",
            "source_name": "智谱AI官方招聘",
            "source_type": "company_official",
            "market_scope": "china",
            "base_url": "https://zhipu-ai.jobs.feishu.cn",
            "allowed_paths": ["/api/v1/search/job/posts", "/index/position/"],
            "collection_mode": "public_json",
            "compliance_status": "approved",
            "compliance_note": "人工复核日期 2026-08-13：公开企业招聘门户。",
            "rate_limit_seconds": 3.0,
            "max_pages": 10,
            "max_records": 300,
            "parser_name": "feishu_company_ats",
            "parser_version": "v1",
            "organization_name": "智谱AI",
            "portal_path": "index",
            "enabled": True,
        }
    )


def make_adapter() -> FeishuATSAdapter:
    source = make_source()
    return FeishuATSAdapter(source=source, registry=SourceRegistry([source]))


def test_build_list_request_uses_reviewed_post_contract():
    request = make_adapter().build_list_request("Python", 20, 20)

    assert request.method == "POST"
    assert request.url == "https://zhipu-ai.jobs.feishu.cn/api/v1/search/job/posts"
    assert request.headers == {
        "Origin": "https://zhipu-ai.jobs.feishu.cn",
        "Referer": "https://zhipu-ai.jobs.feishu.cn/",
        "Portal-Channel": "office",
        "Portal-Platform": "pc",
        "website-path": "index",
    }
    assert request.json_body == {
        "keyword": "Python",
        "limit": 20,
        "offset": 20,
        "portal_type": 2,
        "job_category_id_list": [],
        "location_code_list": [],
        "subject_id_list": [],
        "recruitment_id_list": [],
        "job_function_id_list": [],
    }


def test_parse_list_and_embedded_detail_preserve_traceable_fields():
    adapter = make_adapter()
    page = adapter.parse_list(
        FIXTURE.read_bytes(),
        "application/json; charset=utf-8",
        expected_offset=0,
        expected_limit=20,
    )

    assert page.total == 2
    assert page.offset == 0
    assert page.limit == 20
    assert page.has_more is False
    assert len(page.items) == 2
    assert page.items[0].job_title == "Python后端工程师"
    assert page.items[1].region == "上海, 北京"

    item = page.items[0]
    url = adapter.build_detail_url(item)
    detail = adapter.parse_detail(b"", item, url)

    assert url == (
        "https://zhipu-ai.jobs.feishu.cn/index/position/"
        "7624476433381869860/detail"
    )
    assert detail["company_name"] == "智谱AI"
    assert detail["published_at"] == "2026-04-03T10:28:58.373000+00:00"
    assert detail["published_at_confidence"] == 0.9
    assert "FastAPI" in detail["job_description_raw"]
    assert detail["adapter_extra"] == {
        "ats_type": "feishu",
        "department": "技术研发",
        "recruitment_type": "社会招聘",
    }


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"code": 1, "data": {}},
        {"code": 0},
        {"code": 0, "data": {"count": 1, "job_post_list": {}}},
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


def test_parse_list_rejects_unsafe_source_record_id():
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["data"]["job_post_list"][0]["id"] = "../secret"

    with pytest.raises(AdapterRecordError, match="id"):
        make_adapter().parse_list(
            json.dumps(document).encode(),
            "application/json",
            expected_offset=0,
            expected_limit=20,
        )


def test_parse_detail_rejects_missing_description():
    adapter = make_adapter()
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["data"]["job_post_list"][0]["description"] = ""
    document["data"]["job_post_list"][0]["requirement"] = ""
    page = adapter.parse_list(
        json.dumps(document).encode(),
        "application/json",
        expected_offset=0,
        expected_limit=20,
    )

    with pytest.raises(AdapterRecordError, match="description"):
        adapter.parse_detail(b"", page.items[0], adapter.build_detail_url(page.items[0]))
