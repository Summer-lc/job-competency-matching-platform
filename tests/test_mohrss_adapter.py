from __future__ import annotations

import html
import json
import re
from pathlib import Path

import httpx
import pytest
from bs4 import BeautifulSoup

from src.job_collection.adapters.base import AdapterRecordError, AdapterStructureError
from src.job_collection.adapters.mohrss import MOHRSSAdapter
from src.job_collection.http_client import BoundedHttpClient
from src.job_collection.models import SourceDefinition
from src.job_collection.source_registry import SourceRegistry, URLScopeError
from src.job_collection.storage import RunStorage


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "mohrss"


async def no_sleep(_seconds: float) -> None:
    return None


def make_source(**overrides) -> SourceDefinition:
    payload = {
        "source_id": "mohrss_public_jobs",
        "source_name": "中国公共招聘网公开岗位",
        "source_type": "public_service",
        "market_scope": "china",
        "base_url": "http://job.mohrss.gov.cn",
        "allowed_paths": ["/cjobs/"],
        "collection_mode": "public_html",
        "compliance_status": "approved",
        "compliance_note": "sanitized fixture source boundary",
        "rate_limit_seconds": 5.0,
        "max_pages": 20,
        "max_records": 1000,
        "parser_name": "mohrss",
        "parser_version": "v1",
        "enabled": True,
    }
    payload.update(overrides)
    return SourceDefinition.model_validate(payload)


def make_adapter(**source_overrides) -> MOHRSSAdapter:
    source = make_source(**source_overrides)
    return MOHRSSAdapter(source=source, registry=SourceRegistry([source]))


def list_content() -> bytes:
    return (FIXTURES / "job-list.html").read_bytes()


def detail_content() -> bytes:
    return (FIXTURES / "job-detail.html").read_bytes()


def test_fixtures_document_sanitized_source_structure_without_contact_values():
    list_fixture = (FIXTURES / "job-list.html").read_text(encoding="utf-8")
    detail_fixture = (FIXTURES / "job-detail.html").read_text(encoding="utf-8")

    for fixture in (list_fixture, detail_fixture):
        header = fixture[:1000]
        assert "SANITIZED TEST ONLY" in header
        assert "2026-08-06" in header
        assert "aae004, aae005, aae006" in header
        assert not re.search(r"1[3-9]\d{9}", fixture)
        assert not re.search(r"[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", fixture)
    assert "/cjobs/jobinfolist/listJobinfolist" in list_fixture[:1000]
    assert "/cjobs/jobinfolist/cb21/showgw?id=<acb200>" in detail_fixture[:1000]
    assert "/cjobs/htmls/cb21gwPages/<acb200>.html" in detail_fixture[:1000]


def mutate_list(
    *,
    records: list[object] | None = None,
    pagination: dict[str, object] | None = None,
    encoded_json: str | None = None,
) -> bytes:
    soup = BeautifulSoup(list_content(), "html.parser")
    hidden = soup.select_one("input#findjoblist[name='findjoblist']")
    assert hidden is not None
    if encoded_json is not None:
        hidden["value"] = encoded_json
    elif records is not None:
        hidden["value"] = html.escape(
            json.dumps(records, ensure_ascii=False), quote=True
        )
    for name, value in (pagination or {}).items():
        node = soup.select_one(f"input#{name}[name='{name}']")
        assert node is not None
        node["value"] = str(value)
    return str(soup).encode("utf-8")


def parsed_records() -> list[dict[str, object]]:
    soup = BeautifulSoup(list_content(), "html.parser")
    hidden = soup.select_one("input#findjoblist[name='findjoblist']")
    assert hidden is not None
    value = hidden.get("value")
    assert isinstance(value, str)
    return json.loads(html.unescape(value))


def first_item(adapter: MOHRSSAdapter | None = None):
    adapter = adapter or make_adapter()
    return adapter.parse_list(list_content(), "text/html; charset=utf-8").items[0]


def test_list_request_is_scoped_get_and_uses_only_reviewed_form_parameters():
    adapter = make_adapter()

    request = adapter.build_list_request(
        {
            "textfield": "Python 后端",
            "searchtype": "gw",
            "AREA": "310000",
            "AREA_name": "上海市",
            "aac011": None,
            "orderType": "1",
        },
        page_no=2,
        limit=10,
    )

    assert request.method == "GET"
    assert request.url == (
        "http://job.mohrss.gov.cn/cjobs/jobinfolist/listJobinfolist"
    )
    assert request.params == {
        "textfield": "Python 后端",
        "searchtype": "gw",
        "AREA": "310000",
        "AREA_name": "上海市",
        "orderType": "1",
        "pageNo": 2,
    }


def test_plain_query_maps_to_reviewed_job_keyword_and_unknown_keys_are_rejected():
    adapter = make_adapter()

    bootstrap = adapter.build_bootstrap_request()
    assert bootstrap.method == "GET"
    assert bootstrap.url == (
        "http://job.mohrss.gov.cn/cjobs/jobinfolist/listJobinfolistIndex"
    )
    assert bootstrap.params == {}

    request = adapter.build_list_request("数据工程师", page_no=1, limit=20)
    assert request.params == {
        "textfield": "数据工程师",
        "searchtype": "gw",
        "orderType": "score",
        "pageNo": 1,
    }

    with pytest.raises(ValueError, match="unknown MOHRSS query parameter"):
        adapter.build_list_request({"unexpected": "value"}, page_no=1, limit=10)
    with pytest.raises(ValueError, match="managed internally"):
        adapter.build_list_request({"pageNo": 3}, page_no=1, limit=10)


@pytest.mark.parametrize(
    ("page_no", "limit"),
    [(0, 20), (21, 20), (True, 20), (1, 0), (1, 21), (1, True)],
)
def test_list_request_rejects_invalid_or_non_fixed_pagination(page_no, limit):
    with pytest.raises(ValueError):
        make_adapter().build_list_request({}, page_no=page_no, limit=limit)


def test_list_request_cannot_cross_page_or_record_budget():
    adapter = make_adapter(max_pages=3, max_records=20)

    assert adapter.build_list_request({}, page_no=2, limit=10).params["pageNo"] == 2
    with pytest.raises(ValueError, match="max_pages"):
        adapter.build_list_request({}, page_no=4, limit=10)
    with pytest.raises(ValueError, match="max_records"):
        adapter.build_list_request({}, page_no=3, limit=10)


def test_list_request_does_not_submit_server_owned_pagination_totals():
    adapter = make_adapter(max_records=9)

    request = adapter.build_list_request({}, page_no=1, limit=9)

    assert "pagecount" not in request.params
    assert "totalpages" not in request.params
    assert "totalcount" not in request.params


def test_parse_list_supports_current_pagecount_as_total_pages_contract():
    records = []
    sample = parsed_records()[0]
    for index in range(20):
        record = dict(sample)
        record["acb200"] = str(200000000000 + index)
        records.append(record)

    page = make_adapter().parse_list(
        mutate_list(
            records=records,
            pagination={
                "pageNo": 1,
                "pagecount": 4,
                "totalpages": 4,
                "totalcount": 61,
            },
        ),
        "text/html; charset=utf-8",
        expected_page_no=1,
        expected_limit=20,
    )

    assert (page.total, page.offset, page.limit, page.has_more) == (61, 0, 20, True)
    assert len(page.items) == 20


def test_parse_list_decodes_entity_json_maps_fields_and_uses_total_for_pagination():
    page = make_adapter().parse_list(
        list_content(),
        "text/html; charset=utf-8",
        expected_page_no=1,
        expected_limit=10,
    )

    assert (page.total, page.offset, page.limit, page.has_more) == (12, 0, 10, True)
    assert len(page.items) == 2
    first, second = page.items
    assert first.source_record_id == "100000000001"
    assert first.job_title == "后端开发工程师（脱敏样本）"
    assert first.company_name == "示例科技公司"
    assert first.region == "上海市浦东新区"
    assert first.industry == "软件和信息技术服务人员"
    assert first.salary == "10000-15000元/月"
    assert first.education == "本科"
    assert first.published_at is None
    assert second.source_record_id == "100000000002"
    assert second.salary == "12000元/月"


def test_short_page_still_has_more_and_empty_page_before_total_is_rejected():
    records = parsed_records()[:1]
    page = make_adapter().parse_list(
        mutate_list(records=records), "text/html", expected_page_no=1
    )
    assert len(page.items) == 1
    assert page.has_more is True

    with pytest.raises(AdapterStructureError, match="empty page"):
        make_adapter().parse_list(mutate_list(records=[]), "text/html")


def test_total_is_capped_by_source_record_budget():
    page = make_adapter(max_records=10).parse_list(
        mutate_list(pagination={"totalcount": 999999, "totalpages": 100000}),
        "text/html",
    )

    assert page.total == 10
    assert page.has_more is False


def test_non_page_aligned_record_budget_is_floored_to_complete_pages():
    adapter = make_adapter(max_records=15)

    page = adapter.parse_list(list_content(), "text/html")

    assert (page.total, page.has_more) == (10, False)
    assert adapter.build_list_request({}, page_no=1, limit=10).params["pageNo"] == 1
    with pytest.raises(ValueError, match="max_records"):
        adapter.build_list_request({}, page_no=2, limit=10)


def test_total_is_capped_by_max_pages_and_last_allowed_page_has_no_more():
    adapter = make_adapter(max_pages=2, max_records=1000)
    pagination = {"totalcount": 999, "totalpages": 100}

    first = adapter.parse_list(
        mutate_list(pagination=pagination),
        "text/html",
        expected_page_no=1,
    )
    second = adapter.parse_list(
        mutate_list(
            pagination={**pagination, "pageNo": 2},
        ),
        "text/html",
        expected_page_no=2,
    )

    assert (first.total, first.has_more) == (20, True)
    assert (second.total, second.has_more) == (20, False)


def test_empty_last_allowed_page_before_capped_total_is_structure_error():
    adapter = make_adapter(max_pages=2, max_records=1000)

    with pytest.raises(AdapterStructureError, match="empty page"):
        adapter.parse_list(
            mutate_list(
                records=[],
                pagination={
                    "pageNo": 2,
                    "totalcount": 999,
                    "totalpages": 100,
                },
            ),
            "text/html",
            expected_page_no=2,
        )


@pytest.mark.parametrize("name", ["pageNo", "pagecount", "totalpages", "totalcount"])
@pytest.mark.parametrize("bad_value", ["", "-1", "1.0", "true"])
def test_pagination_hidden_values_are_strict_integers(name, bad_value):
    with pytest.raises(AdapterStructureError, match="pagination"):
        make_adapter().parse_list(
            mutate_list(pagination={name: bad_value}), "text/html"
        )


@pytest.mark.parametrize("name", ["pageNo"])
def test_page_number_and_size_must_be_positive(name):
    with pytest.raises(AdapterStructureError, match="pagination"):
        make_adapter().parse_list(
            mutate_list(pagination={name: 0}), "text/html"
        )


def test_current_zero_result_allows_zero_pagecount_and_totals():
    page = make_adapter().parse_list(
        mutate_list(
            records=[],
            pagination={
                "pageNo": 1,
                "pagecount": 0,
                "totalpages": 0,
                "totalcount": 0,
            },
        ),
        "text/html; charset=utf-8",
        expected_page_no=1,
        expected_limit=20,
    )

    assert (page.items, page.total, page.offset, page.limit, page.has_more) == (
        (),
        0,
        0,
        20,
        False,
    )


@pytest.mark.parametrize("totalpages", [0, 1])
def test_first_page_zero_result_is_valid(totalpages):
    page = make_adapter().parse_list(
        mutate_list(
            records=[],
            pagination={"totalcount": 0, "totalpages": totalpages},
        ),
        "text/html",
        expected_page_no=1,
    )

    assert (page.items, page.total, page.offset, page.has_more) == ((), 0, 0, False)


def test_nonfirst_zero_result_page_is_a_pagination_contradiction():
    with pytest.raises(AdapterStructureError, match="zero-result"):
        make_adapter().parse_list(
            mutate_list(
                records=[],
                pagination={"pageNo": 2, "totalcount": 0, "totalpages": 1},
            ),
            "text/html",
        )


def test_list_items_cannot_exceed_server_total():
    with pytest.raises(AdapterStructureError, match="server total"):
        make_adapter().parse_list(
            mutate_list(pagination={"totalcount": 1, "totalpages": 1}),
            "text/html",
        )


def test_last_page_items_cannot_exceed_server_remaining_count():
    with pytest.raises(AdapterStructureError, match="remaining"):
        make_adapter().parse_list(
            mutate_list(
                pagination={"pageNo": 2, "totalcount": 11, "totalpages": 2}
            ),
            "text/html",
            expected_page_no=2,
        )


def test_response_page_must_match_expected_page_and_internal_totals():
    adapter = make_adapter()
    with pytest.raises(AdapterStructureError, match="requested page"):
        adapter.parse_list(list_content(), "text/html", expected_page_no=2)
    with pytest.raises(AdapterStructureError, match="totalpages"):
        adapter.parse_list(
            mutate_list(pagination={"totalpages": 3}), "text/html"
        )


@pytest.mark.parametrize(
    "payload",
    ["not-json", "{}", "null", "NaN", "[NaN]", "[Infinity]", "[-Infinity]"],
)
def test_parse_list_rejects_bad_non_array_or_non_finite_json(payload):
    with pytest.raises(AdapterStructureError):
        make_adapter().parse_list(
            mutate_list(encoded_json=html.escape(payload, quote=True)), "text/html"
        )


def test_parse_list_requires_one_exact_findjoblist_hidden_field_and_html_content():
    adapter = make_adapter()
    duplicate = list_content().replace(
        b"</form>",
        b'<input type="hidden" id="findjoblist" name="findjoblist" value="[]"></form>',
    )
    missing = list_content().replace(b'id="findjoblist"', b'id="renamed"')

    with pytest.raises(AdapterStructureError, match="exactly one"):
        adapter.parse_list(duplicate, "text/html")
    with pytest.raises(AdapterStructureError, match="exactly one"):
        adapter.parse_list(missing, "text/html")
    with pytest.raises(AdapterStructureError, match="HTML content"):
        adapter.parse_list(list_content(), "application/json")


@pytest.mark.parametrize("bad_id", [None, "", "0", "-1", True, "12x", "9" * 33])
def test_list_primary_key_contract_rejects_missing_or_unsafe_acb200(bad_id):
    records = parsed_records()
    if bad_id is None:
        records[0].pop("acb200")
    else:
        records[0]["acb200"] = bad_id

    with pytest.raises(AdapterStructureError, match="acb200"):
        make_adapter().parse_list(mutate_list(records=records), "text/html")


def test_salary_invalid_values_are_not_fabricated_and_are_audited():
    adapter = make_adapter()
    records = parsed_records()
    records[0].update({"acb241": 16000, "acb242": 15000})
    item = adapter.parse_list(mutate_list(records=records), "text/html").items[0]

    assert item.salary is None
    detail = adapter.parse_detail(
        detail_content(), item, adapter.static_detail_url(item)
    )
    assert any(
        issue["field"] == "salary"
        for issue in detail["adapter_extra"]["validation_issues"]
    )


@pytest.mark.parametrize(
    "unit_fields",
    [
        {"acb239": "1"},
        {},
        {"acb239_": "元/周"},
    ],
)
def test_unknown_or_missing_salary_unit_is_not_fabricated_and_is_audited(
    unit_fields,
):
    adapter = make_adapter()
    records = parsed_records()
    records[0].pop("acb239_")
    records[0].update(unit_fields)

    item = adapter.parse_list(
        mutate_list(records=records), "text/html"
    ).items[0]
    detail = adapter.parse_detail(
        detail_content(), item, adapter.static_detail_url(item)
    )

    assert item.salary is None
    issue = next(
        issue
        for issue in detail["adapter_extra"]["validation_issues"]
        if issue["field"] == "salary"
    )
    assert issue["code"] == "salary_unit_unrecognized"
    assert issue["raw"]["unit"] == {
        "acb239_": unit_fields.get("acb239_"),
        "acb239_t": None,
        "acb239": unit_fields.get("acb239"),
    }


def test_explicit_detail_salary_with_confirmed_unit_can_supply_salary():
    adapter = make_adapter()
    records = parsed_records()
    records[0].pop("acb239_")
    item = adapter.parse_list(mutate_list(records=records), "text/html").items[0]
    page = """
    <html><title>中国公共招聘网_招聘岗位</title><body>
      <div><span>薪资待遇</span><strong>10000 元以上/月</strong></div>
      <div class="gwmsDiv"><div id="gwms">岗位职责：负责后端开发。<br>
      岗位要求：熟悉 Python。</div></div>
    </body></html>
    """

    record = adapter.parse_detail(
        page.encode("utf-8"), item, adapter.static_detail_url(item)
    )

    assert item.salary is None
    assert record["salary"] == "10000元以上/月"


def test_detail_initial_and_static_urls_keep_scope_and_same_numeric_job_id():
    adapter = make_adapter()
    item = first_item(adapter)
    initial = adapter.build_detail_url(item)
    final = adapter.static_detail_url(item)

    assert initial == (
        "http://job.mohrss.gov.cn/cjobs/jobinfolist/cb21/showgw"
        "?id=100000000001"
    )
    assert final == (
        "http://job.mohrss.gov.cn/cjobs/htmls/cb21gwPages/100000000001.html"
    )
    assert adapter.validate_detail_redirect(item, initial, final) is None
    assert adapter.validate_detail_url(item, initial) == initial
    assert adapter.validate_detail_url(item, final) == final


def test_detail_redirect_rejects_reverse_direction():
    adapter = make_adapter()
    item = first_item(adapter)

    with pytest.raises(AdapterRecordError, match="initial.*static"):
        adapter.validate_detail_redirect(
            item,
            adapter.static_detail_url(item),
            adapter.build_detail_url(item),
        )


@pytest.mark.asyncio
async def test_http_client_rejects_wrong_id_redirect_before_second_request(tmp_path):
    adapter = make_adapter()
    item = first_item(adapter)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("wrong-id redirect must be rejected before request")
        return httpx.Response(
            302,
            headers={
                "location": "/cjobs/htmls/cb21gwPages/100000000002.html"
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    bounded = BoundedHttpClient(
        source=adapter.source,
        registry=adapter.registry,
        storage=RunStorage(tmp_path / "collections", "run-mohrss-redirect"),
        client=http_client,
    )
    try:
        with pytest.raises(AdapterRecordError, match="does not match"):
            await bounded.fetch(
                adapter.build_detail_url(item),
                resume=False,
                redirect_validator=lambda current_url, location: (
                    adapter.validate_detail_redirect(
                        item, current_url, location
                    )
                ),
            )
    finally:
        await http_client.aclose()

    assert calls == 1


@pytest.mark.asyncio
async def test_offline_initial_redirect_callback_and_final_detail_parse(tmp_path):
    adapter = make_adapter()
    item = first_item(adapter)
    initial_url = adapter.build_detail_url(item)
    final_url = adapter.static_detail_url(item)
    seen_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(str(request.url))
        if request.url.path == adapter.initial_detail_path:
            return httpx.Response(
                302,
                headers={
                    "location": "../../htmls/cb21gwPages/100000000001.html"
                },
            )
        return httpx.Response(
            200,
            content=detail_content(),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    bounded = BoundedHttpClient(
        source=adapter.source,
        registry=adapter.registry,
        storage=RunStorage(tmp_path / "collections", "run-mohrss-success"),
        client=http_client,
        sleep=no_sleep,
    )
    try:
        fetched = await bounded.fetch(
            initial_url,
            resume=False,
            redirect_validator=lambda current_url, canonical_target: (
                adapter.validate_detail_redirect(
                    item, current_url, canonical_target
                )
            ),
        )
    finally:
        await http_client.aclose()

    record = adapter.parse_detail(fetched.content, item, fetched.final_url)
    assert seen_requests == [initial_url, final_url]
    assert fetched.final_url == final_url
    assert record["source_url"] == final_url
    assert "Java & Spring Boot" in record["job_description_raw"]


@pytest.mark.parametrize(
    "url",
    [
        "http://job.mohrss.gov.cn/cjobs/jobinfolist/cb21/showgw?id=100000000002",
        "http://job.mohrss.gov.cn/cjobs/jobinfolist/cb21/showgw?id",
        "http://job.mohrss.gov.cn/cjobs/htmls/cb21gwPages/100000000002.html",
        "http://job.mohrss.gov.cn/cjobs/htmls/cb21gwPages/100000000001.htm",
        "http://job.mohrss.gov.cn/cjobs/htmls/cb21gwPages/100000000001.html?x=1",
        "http://evil.example/cjobs/htmls/cb21gwPages/100000000001.html",
    ],
)
def test_detail_url_rejects_wrong_id_shape_or_scope(url):
    with pytest.raises((AdapterRecordError, URLScopeError)):
        make_adapter().validate_detail_url(first_item(), url)


def test_detail_maps_jd_without_dates_or_contact_area_and_preserves_technical_text():
    adapter = make_adapter()
    item = first_item(adapter)
    url = adapter.static_detail_url(item)

    record = adapter.parse_detail(detail_content(), item, url)

    assert record["source_id"] == "mohrss_public_jobs"
    assert record["source_record_id"] == "100000000001"
    assert record["job_title"] == "后端开发工程师（脱敏样本）"
    assert record["company_name"] == "示例科技公司"
    assert record["region"] == "上海市浦东新区"
    assert record["industry"] == "软件和信息技术服务人员"
    assert record["salary"] == "10000-15000元/月"
    assert record["education"] == "本科"
    assert record["experience"] is None
    assert record["published_at"] is None
    assert record["published_at_evidence"] is None
    assert record["published_at_confidence"] == 0.0
    assert record["confidence"] == 0.0
    assert "岗位职责：\n1." in record["job_description_raw"]
    assert "\n\n岗位要求：\n" in record["job_description_raw"]
    assert "List<String>" in record["job_description_raw"]
    assert "a < b" in record["job_description_raw"]
    assert "Java & Spring Boot" in record["job_description_raw"]
    assert record["page_title"] == "中国公共招聘网_招聘岗位"
    assert record["response_status"] == 200
    assert record["source_url"] == url
    assert record["adapter_extra"]["source_dates"] == {
        "s_ctime": "2026-08-01 09:00:00",
        "s_uptime": "2026-08-04 10:30:00",
        "s_aae395": "2026-08-01",
    }

    serialized = json.dumps(record, ensure_ascii=False)
    for forbidden in (
        "aae004",
        "aae005",
        "aae006",
        "示例联系人",
        "13800138000",
        "脱敏前详细地址已移除",
        "测试值已移除",
    ):
        assert forbidden not in serialized


def test_jd_redacts_obvious_contact_lines_but_keeps_job_duties():
    adapter = make_adapter()
    item = first_item(adapter)
    page = """
    <html><title>中国公共招聘网_招聘岗位</title><body>
      <div class="gwmsDiv"><div id="gwms">岗位职责：负责后端开发。<br>
      联系人：示例联系人<br>电话：13800138000<br>邮箱：fixture@example.test<br>
      岗位要求：熟悉 Python。</div></div>
    </body></html>
    """

    record = adapter.parse_detail(
        page.encode("utf-8"), item, adapter.static_detail_url(item)
    )

    assert "负责后端开发" in record["job_description_raw"]
    assert "熟悉 Python" in record["job_description_raw"]
    assert not re.search(r"13800138000|fixture@example\.test|示例联系人", json.dumps(record, ensure_ascii=False))
    assert any(
        issue["field"] == "job_description_raw"
        for issue in record["adapter_extra"]["validation_issues"]
    )


def test_unknown_and_nested_raw_fields_cannot_reintroduce_contact_pii():
    adapter = make_adapter()
    records = parsed_records()
    records[0].update(
        {
            "contactPerson": "示例联系人乙",
            "fixture_note": "联系人：示例联系人丙",
            "nested": {
                "recruiterEmail": "fixture@example.test",
                "safe": "公开岗位补充说明",
            },
        }
    )
    item = adapter.parse_list(mutate_list(records=records), "text/html").items[0]
    record = adapter.parse_detail(
        detail_content(), item, adapter.static_detail_url(item)
    )

    serialized = json.dumps(record, ensure_ascii=False)
    assert "示例联系人乙" not in serialized
    assert "示例联系人丙" not in serialized
    assert "fixture@example.test" not in serialized
    assert "公开岗位补充说明" in serialized


def test_aae_contact_keys_with_suffixes_are_removed_recursively_case_insensitively():
    adapter = make_adapter()
    records = parsed_records()
    records[0].update(
        {
            "AAE004_private": "示例联系人甲",
            "nested": {
                "aae005_note": "13800138001",
                "items": [
                    {
                        "AaE006_extra": "示例详细联系地址",
                        "safe": "保留的岗位信息",
                    }
                ],
            },
        }
    )
    item = adapter.parse_list(mutate_list(records=records), "text/html").items[0]
    record = adapter.parse_detail(
        detail_content(), item, adapter.static_detail_url(item)
    )

    serialized = json.dumps(record, ensure_ascii=False)
    for forbidden in (
        "AAE004_private",
        "aae005_note",
        "AaE006_extra",
        "示例联系人甲",
        "13800138001",
        "示例详细联系地址",
    ):
        assert forbidden not in serialized
    assert "保留的岗位信息" in serialized


def test_pii_key_tokens_drop_sensitive_fields_without_deleting_safe_lookalikes():
    adapter = make_adapter()
    records = parsed_records()
    sensitive = {
        "contactPerson": "敏感联系人甲",
        "person_name": "敏感联系人乙",
        "primary_phone": "13800138010",
        "candidate_mobile": "13800138011",
        "recruiterEmail": "private@example.test",
        "backup_mail": "private2@example.test",
        "wechat_id": "private-wechat",
        "weixin": "private-weixin",
        "qq_number": "123456789",
        "liaison": "敏感联络值",
        "phoneBook": "敏感电话簿",
        "wechatOpenId": "敏感微信标识",
        "mailMergeConfig": "敏感邮件配置",
        "recruiterName": "敏感招聘人姓名",
        "recruiter": "敏感招聘人",
        "name": "敏感未知姓名",
        "candidateAddress": "敏感候选人地址",
        "wechatAccount": "敏感微信账号",
        "qqNumber": "987654321",
        "联系人": "敏感联系人丙",
        "招聘联系人": "敏感招聘联系人",
        "联系人姓名": "敏感联系人姓名",
        "联系电话": "13800138012",
        "手机号码": "13800138013",
        "邮箱": "private3@example.test",
        "微信": "private-wechat-2",
        "QQ号": "private-qq-number",
        "联络人": "敏感联络人",
        "招聘负责人": "敏感招聘负责人",
        "HR姓名": "敏感人事姓名",
        "经办人": "敏感经办人",
        "招聘负责人_projectName": "敏感混合角色姓名",
    }
    safe = {
        "office_tel": "办公电话系统字段",
        "officeAddress": "园区层级区域说明",
        "addressableMarket": "企业服务市场",
        "mobileDevelopmentSkills": "Android 与 iOS",
        "emailTemplate": "通知模板名称",
        "contactlessDelivery": "无接触交付能力",
        "mobilePlatformExperience": "移动平台经验",
        "emailMarketingSkills": "邮件营销技能",
        "mailServerExperience": "邮件服务器经验",
        "telephoneSystemKnowledge": "电话系统知识",
        "qqProtocolExperience": "QQ 协议经验",
        "邮箱系统经验": "企业邮箱运维经验",
        "移动开发技能": "跨平台移动开发",
        "skillName": "数据分析技能",
        "project_name": "智能招聘项目",
        "industry-name": "软件和信息技术服务业",
        "jobName": "后端开发工程师",
        "companyName": "示例科技公司",
        "categoryName": "专业技术人员",
        "occupationName": "软件工程技术人员",
        "technologyName": "容器编排技术",
    }
    records[0]["nested_tokens"] = [{**sensitive, **safe}]

    item = adapter.parse_list(mutate_list(records=records), "text/html").items[0]
    record = adapter.parse_detail(
        detail_content(), item, adapter.static_detail_url(item)
    )
    serialized = json.dumps(record, ensure_ascii=False)
    finding_paths = {
        finding["path"]
        for finding in record["adapter_extra"]["pii_filter_findings"]
    }
    assert all(
        set(finding) == {"path", "reason"}
        for finding in record["adapter_extra"]["pii_filter_findings"]
    )

    for key, value in sensitive.items():
        assert value not in serialized
        assert f"nested_tokens[0].{key}" in finding_paths
    for key, value in safe.items():
        assert key in serialized
        assert value in serialized


def test_unknown_suspected_pii_key_records_only_path_and_reason_without_value():
    adapter = make_adapter()
    records = parsed_records()
    records[0]["nested"] = {
        "contact_details_blob": "unknown-sensitive-payload",
        "safe": "公开岗位说明",
    }

    item = adapter.parse_list(mutate_list(records=records), "text/html").items[0]
    record = adapter.parse_detail(
        detail_content(), item, adapter.static_detail_url(item)
    )

    serialized = json.dumps(record, ensure_ascii=False)
    assert "unknown-sensitive-payload" not in serialized
    assert record["adapter_extra"]["pii_filter_findings"] == [
        {
            "path": "nested.contact_details_blob",
            "reason": "unrecognized_contact_key",
        }
    ]
    assert set(record["adapter_extra"]["pii_filter_findings"][0]) == {
        "path",
        "reason",
    }
    assert "公开岗位说明" in serialized


@pytest.mark.parametrize(
    "page",
    [
        "<html><title>中国公共招聘网_招聘岗位</title><body></body></html>",
        "<html><title>中国公共招聘网_招聘岗位</title><body><div class='gwmsDiv'><div id='gwms'></div></div></body></html>",
        "<html><title>中国公共招聘网_招聘岗位</title><body><div class='gwmsDiv'><div id='gwms'>one</div><div id='gwms'>two</div></div></body></html>",
        "<html><title>用户登录</title><body><div class='gwmsDiv'><div id='gwms'>岗位职责：负责开发。</div></div></body></html>",
        "<html><title>中国公共招聘网_招聘岗位</title><body><div class='gwmsDiv'><form action='/login'><input type='password'><div id='gwms'>请先登录后查看岗位</div></form></div></body></html>",
        "<html><title>中国公共招聘网_招聘岗位</title><body><div class='gwmsDiv'><div id='gwms'>404 Not Found</div></div></body></html>",
        "<html><title>系统错误</title><body><div class='gwmsDiv'><div id='gwms'>岗位职责：负责开发。</div></div></body></html>",
    ],
)
def test_detail_rejects_missing_ambiguous_empty_wrong_or_gate_pages(page):
    adapter = make_adapter()
    item = first_item(adapter)
    with pytest.raises((AdapterStructureError, AdapterRecordError)):
        adapter.parse_detail(
            page.encode("utf-8"), item, adapter.static_detail_url(item)
        )


@pytest.mark.parametrize(
    "gate_text",
    [
        "请先登录后查看岗位",
        "请登录后查看职位",
        "登录后查看岗位",
        "请先登录后查看岗位详情",
        "请登录后查看职位信息",
        "登录后查看详情",
        "登录后查看信息",
        "访问受限",
    ],
)
def test_unique_short_jd_with_exact_gate_phrase_is_rejected_without_form(
    gate_text,
):
    adapter = make_adapter()
    item = first_item(adapter)
    page = (
        "<html><title>中国公共招聘网_招聘岗位</title><body>"
        f"<div class='gwmsDiv'><div id='gwms'>{gate_text}</div></div>"
        "</body></html>"
    )

    with pytest.raises(AdapterStructureError, match="login or verification"):
        adapter.parse_detail(
            page.encode("utf-8"), item, adapter.static_detail_url(item)
        )


def test_legal_authentication_job_text_and_global_login_link_are_not_a_gate():
    adapter = make_adapter()
    item = first_item(adapter)
    page = """
    <html><title>中国公共招聘网_招聘岗位</title><body>
      <a href="/cjobs/login">用户登录</a>
      <div class="gwmsDiv"><div id="gwms">岗位职责：负责登录认证和 CAPTCHA 平台开发。<br>
      岗位要求：熟悉 OAuth、身份验证和 Web 安全。</div></div>
    </body></html>
    """

    record = adapter.parse_detail(
        page.encode("utf-8"), item, adapter.static_detail_url(item)
    )
    assert "登录认证" in record["job_description_raw"]
    assert "CAPTCHA" in record["job_description_raw"]


def test_legal_jd_implementing_login_required_resume_view_is_not_a_gate():
    adapter = make_adapter()
    item = first_item(adapter)
    page = """
    <html><title>中国公共招聘网_招聘岗位</title><body>
      <div class="gwmsDiv"><div id="gwms">岗位职责：负责实现登录后查看简历功能，并完善权限审计。<br>
      岗位要求：熟悉 Python、OAuth 和访问控制。</div></div>
    </body></html>
    """

    record = adapter.parse_detail(
        page.encode("utf-8"), item, adapter.static_detail_url(item)
    )

    assert "负责实现登录后查看简历功能" in record["job_description_raw"]


def test_legal_fault_diagnosis_jd_with_http_error_terms_is_not_an_error_page():
    adapter = make_adapter()
    item = first_item(adapter)
    page = """
    <html><title>中国公共招聘网_招聘岗位</title><body>
      <div class="gwmsDiv"><div id="gwms">岗位职责：诊断 API 返回的 404 Not Found 与 403 Forbidden，完善监控告警和故障恢复流程。<br>
      岗位要求：熟悉 HTTP、Python 和分布式系统排障。</div></div>
    </body></html>
    """

    record = adapter.parse_detail(
        page.encode("utf-8"), item, adapter.static_detail_url(item)
    )

    assert "404 Not Found" in record["job_description_raw"]
    assert "403 Forbidden" in record["job_description_raw"]


def test_adapter_requires_registered_fixed_source_definition():
    source = make_source()
    altered = make_source(base_url="http://mirror.example.test")
    registry = SourceRegistry([source])

    with pytest.raises(ValueError, match="registered SourceDefinition"):
        MOHRSSAdapter(source=altered, registry=registry)
