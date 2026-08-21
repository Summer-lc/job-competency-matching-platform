import json
from pathlib import Path

import pytest

from src.job_collection.adapters.base import (
    AdapterRecordError,
    AdapterStructureError,
)
from src.job_collection.adapters.ncss import NCSSAdapter
from src.job_collection.models import SourceDefinition
from src.job_collection.source_registry import SourceRegistry, URLScopeError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures" / "ncss"


def make_source(**overrides) -> SourceDefinition:
    payload = {
        "source_id": "ncss_public_jobs",
        "source_name": "国家大学生就业服务平台公开岗位",
        "source_type": "university_recruitment",
        "market_scope": "china",
        "base_url": "https://cnu.ncss.cn",
        "allowed_paths": ["/student/jobs/"],
        "collection_mode": "public_json",
        "compliance_status": "approved",
        "compliance_note": "fixture source boundary",
        "rate_limit_seconds": 3.0,
        "max_pages": 20,
        "max_records": 1000,
        "parser_name": "ncss",
        "parser_version": "v1",
        "enabled": True,
    }
    payload.update(overrides)
    return SourceDefinition.model_validate(payload)


def make_adapter(**source_overrides) -> NCSSAdapter:
    source = make_source(**source_overrides)
    return NCSSAdapter(source=source, registry=SourceRegistry([source]))


def list_content() -> bytes:
    return (FIXTURES / "jobs-list.json").read_bytes()


def detail_content() -> bytes:
    return (FIXTURES / "job-detail.html").read_bytes()


def test_list_request_is_scoped_get_with_known_non_null_parameters():
    adapter = make_adapter()

    request = adapter.build_list_request(
        {
            "jobName": "Python 后端",
            "areaCode": "310000",
            "degreeCode": None,
            "recruitType": "1",
        },
        offset=20,
        limit=25,
    )

    assert request.method == "GET"
    assert request.url == "https://cnu.ncss.cn/student/jobs/jobslist/ajax/"
    assert request.params == {
        "jobName": "Python 后端",
        "areaCode": "310000",
        "recruitType": "1",
        "offset": 20,
        "limit": 25,
    }
    assert all(value is not None for value in request.params.values())


def test_plain_query_maps_to_job_name_and_unknown_query_keys_are_rejected():
    adapter = make_adapter()

    request = adapter.build_list_request("数据工程师", offset=0, limit=10)
    assert request.params["jobName"] == "数据工程师"

    with pytest.raises(ValueError, match="unknown NCSS query parameter"):
        adapter.build_list_request({"unexpected": "value"}, offset=0, limit=10)


@pytest.mark.parametrize(
    ("offset", "limit"),
    [(-1, 10), (0, 0), (0, 101), (True, 10), (0, True)],
)
def test_list_request_rejects_invalid_pagination(offset, limit):
    adapter = make_adapter()

    with pytest.raises(ValueError):
        adapter.build_list_request({}, offset=offset, limit=limit)


def test_list_request_honors_smaller_source_record_limit():
    adapter = make_adapter(max_records=40)

    with pytest.raises(ValueError, match="limit"):
        adapter.build_list_request({}, offset=0, limit=41)


@pytest.mark.parametrize(("offset", "limit"), [(40, 1), (41, 1), (30, 11)])
def test_list_request_cannot_cross_the_source_record_budget(offset, limit):
    adapter = make_adapter(max_records=40)

    with pytest.raises(ValueError, match="max_records"):
        adapter.build_list_request({}, offset=offset, limit=limit)

    request = adapter.build_list_request({}, offset=39, limit=1)
    assert request.params["offset"] + request.params["limit"] == 40


def test_parse_list_maps_fields_salary_boundaries_dates_and_pagination():
    page = make_adapter().parse_list(
        list_content(),
        "application/json; charset=utf-8",
        expected_offset=0,
        expected_limit=2,
    )

    assert (page.total, page.offset, page.limit, page.has_more) == (3, 0, 2, True)
    assert len(page.items) == 2
    first, second = page.items
    assert first.source_record_id == "fixture-job-001"
    assert first.job_title == "后端开发工程师（脱敏样本）"
    assert first.company_name == "示例科技有限公司"
    assert first.region == "上海市"
    assert first.industry == "软件和信息技术服务业"
    assert first.salary == "10000-15000元/月"
    assert first.education == "本科"
    assert first.published_at == "2026-08-04"
    assert second.salary == "最高12000元/月"
    assert second.education is None
    assert second.published_at is None


def test_parse_list_formats_low_only_salary_without_inventing_a_high_bound():
    document = json.loads(list_content())
    document["data"]["list"][1]["lowMonthPay"] = 8000
    document["data"]["list"][1]["highMonthPay"] = None

    page = make_adapter().parse_list(
        json.dumps(document).encode("utf-8"), "application/json"
    )

    assert page.items[1].salary == "最低8000元/月"


@pytest.mark.parametrize(
    ("low", "high"),
    [
        (True, 15000),
        (-1, 15000),
        ("NaN", 15000),
        (16000, 15000),
    ],
)
def test_invalid_salary_is_not_fabricated_and_is_audited(low, high):
    adapter = make_adapter()
    document = json.loads(list_content())
    document["data"]["list"][0].update(
        {"lowMonthPay": low, "highMonthPay": high}
    )
    page = adapter.parse_list(
        json.dumps(document).encode("utf-8"), "application/json"
    )

    item = page.items[0]
    assert item.salary is None
    record = adapter.parse_detail(
        detail_content(), item, adapter.build_detail_url(item)
    )
    assert record["adapter_extra"]["lowMonthPay"] == low
    assert record["adapter_extra"]["highMonthPay"] == high
    assert any(
        issue["field"] == "salary"
        for issue in record["adapter_extra"]["validation_issues"]
    )


@pytest.mark.parametrize(
    "publish_date",
    [
        "2026-08-04",
        "2026/08/04",
        "2026-08-04 09:30",
        "2026-08-04 09:30:45",
    ],
)
def test_approved_publish_date_formats_receive_explicit_evidence(publish_date):
    adapter = make_adapter()
    document = json.loads(list_content())
    document["data"]["list"][0]["publishDate"] = publish_date
    item = adapter.parse_list(
        json.dumps(document).encode("utf-8"), "application/json"
    ).items[0]

    record = adapter.parse_detail(
        detail_content(), item, adapter.build_detail_url(item)
    )

    assert record["published_at"] == publish_date
    assert record["published_at_confidence"] == 0.9
    assert publish_date in record["published_at_evidence"]


@pytest.mark.parametrize(
    "publish_date",
    ["2026-02-30", "2026-8-4", "2026-08-04T09:30:00", "not-a-date", True],
)
def test_invalid_publish_date_is_untrusted_and_audited(publish_date):
    adapter = make_adapter()
    document = json.loads(list_content())
    document["data"]["list"][0]["publishDate"] = publish_date
    item = adapter.parse_list(
        json.dumps(document).encode("utf-8"), "application/json"
    ).items[0]

    assert item.published_at is None
    record = adapter.parse_detail(
        detail_content(), item, adapter.build_detail_url(item)
    )
    assert record["published_at"] is None
    assert record["published_at_evidence"] is None
    assert record["published_at_confidence"] == 0.0
    assert record["adapter_extra"]["publishDate"] == publish_date
    assert any(
        issue["field"] == "publishDate"
        for issue in record["adapter_extra"]["validation_issues"]
    )


def test_detail_maps_raw_record_and_keeps_publication_and_update_dates_separate():
    adapter = make_adapter()
    page = adapter.parse_list(list_content(), "application/json")
    url = adapter.build_detail_url(page.items[0])

    record = adapter.parse_detail(detail_content(), page.items[0], url)

    assert record["source_record_id"] == "fixture-job-001"
    assert record["job_title"] == "后端开发工程师（脱敏样本）"
    assert record["company_name"] == "示例科技有限公司"
    assert record["published_at"] == "2026-08-04"
    assert record["published_at_evidence"] == "NCSS列表字段 publishDate: 2026-08-04"
    assert record["published_at_confidence"] >= 0.8
    assert record["confidence"] == record["published_at_confidence"]
    assert record["experience"] == "1-3年"
    assert record["page_title"].startswith("后端开发工程师")
    assert record["response_status"] == 200
    assert record["source_url"] == url
    assert record["adapter_extra"]["updateDate"] == "2026-08-05"
    assert record["adapter_extra"]["fixtureExtraField"] == "preserved"
    assert record["adapter_extra"]["headCount"] == 2
    assert record["adapter_extra"]["recTags"] == ["fixture", "sanitized"]


def test_detail_uses_explicit_company_industry_when_list_omits_it():
    adapter = make_adapter()
    document = json.loads(list_content())
    document["data"]["list"][0].pop("industrySectorsName", None)
    item = adapter.parse_list(
        json.dumps(document).encode("utf-8"), "application/json"
    ).items[0]
    detail = detail_content().replace(
        b"</body>",
        '<span id="mainindustries">计算机软件</span></body>'.encode("utf-8"),
    )

    record = adapter.parse_detail(detail, item, adapter.build_detail_url(item))

    assert record["industry"] == "计算机软件"


def test_update_date_alone_never_becomes_published_at():
    adapter = make_adapter()
    item = adapter.parse_list(list_content(), "application/json").items[1]
    record = adapter.parse_detail(detail_content(), item, adapter.build_detail_url(item))

    assert record["published_at"] is None
    assert record["published_at_evidence"] is None
    assert record["published_at_confidence"] == 0.0
    assert record["adapter_extra"]["updateDate"] == "2026-08-03"


def test_detail_text_preserves_lines_entities_and_technical_expressions():
    adapter = make_adapter()
    item = adapter.parse_list(list_content(), "application/json").items[0]
    record = adapter.parse_detail(detail_content(), item, adapter.build_detail_url(item))

    description = record["job_description_raw"]
    assert "岗位职责：\n1." in description
    assert "\n\n岗位要求：\n" in description
    assert "List<String>" in description
    assert "a < b" in description
    assert "AT&T API" in description
    assert "Java & Spring Boot" in description


@pytest.mark.parametrize(
    "job_id",
    ["", "   ", ".", "..", "../escape", "a/b", "a\\b", "bad\nvalue", "bad\x7fvalue"],
)
def test_detail_url_rejects_unsafe_job_ids(job_id):
    adapter = make_adapter()
    item = adapter.parse_list(list_content(), "application/json").items[0]
    unsafe = item.model_copy(update={"source_record_id": job_id})

    with pytest.raises((AdapterRecordError, URLScopeError)):
        adapter.build_detail_url(unsafe)


def test_detail_url_encodes_safe_opaque_job_id_and_validates_registry_scope():
    adapter = make_adapter()
    item = adapter.parse_list(list_content(), "application/json").items[0]
    encoded = item.model_copy(update={"source_record_id": "岗位 001"})

    assert adapter.build_detail_url(encoded) == (
        "https://cnu.ncss.cn/student/jobs/%E5%B2%97%E4%BD%8D%20001/detail.html"
    )


@pytest.mark.parametrize(
    "mutator",
    [
        lambda doc: doc.pop("data"),
        lambda doc: doc["data"].pop("list"),
        lambda doc: doc["data"].pop("pagenation"),
        lambda doc: doc["data"].update({"list": {}}),
        lambda doc: doc["data"]["list"][0].pop("jobId"),
        lambda doc: doc["data"].update({"pagenation": []}),
        lambda doc: doc["data"]["pagenation"].update({"total": "3"}),
    ],
)
def test_parse_list_rejects_batch_structure_anomalies(mutator):
    document = json.loads(list_content())
    mutator(document)

    with pytest.raises(AdapterStructureError):
        make_adapter().parse_list(json.dumps(document).encode(), "application/json")


def test_parse_list_rejects_non_json_and_wrong_content_type():
    adapter = make_adapter()

    with pytest.raises(AdapterStructureError):
        adapter.parse_list(b"not-json", "application/json")
    with pytest.raises(AdapterStructureError):
        adapter.parse_list(list_content(), "text/html")


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_parse_list_rejects_non_finite_json_constants(constant):
    payload = list_content().replace(b'"sanitized"', constant.encode("ascii"), 1)

    with pytest.raises(AdapterStructureError, match="invalid JSON"):
        make_adapter().parse_list(payload, "application/json")


def test_server_total_is_bounded_and_cannot_force_phantom_pages():
    document = json.loads(list_content())
    document["data"]["pagenation"]["total"] = 999999999
    adapter = make_adapter(max_records=2)

    page = adapter.parse_list(json.dumps(document).encode(), "application/json")

    assert page.total == 2
    assert page.has_more is False


@pytest.mark.parametrize(
    ("expected_offset", "expected_limit"), [(2, 2), (0, 10)]
)
def test_response_pagination_must_match_the_requested_page(
    expected_offset, expected_limit
):
    with pytest.raises(AdapterStructureError, match="does not match"):
        make_adapter().parse_list(
            list_content(),
            "application/json",
            expected_offset=expected_offset,
            expected_limit=expected_limit,
        )


def test_short_page_ends_query_when_server_reports_a_phantom_total():
    document = json.loads(list_content())
    document["data"]["list"] = document["data"]["list"][:1]
    document["data"]["pagenation"]["total"] = 3

    page = make_adapter().parse_list(
        json.dumps(document).encode("utf-8"),
        "application/json",
        expected_offset=0,
        expected_limit=2,
    )

    assert len(page.items) == 1
    assert page.has_more is False


def test_empty_first_page_ends_a_zero_result_query_despite_phantom_total():
    document = json.loads(list_content())
    document["data"]["list"] = []
    document["data"]["pagenation"]["total"] = 3

    page = make_adapter().parse_list(
        json.dumps(document).encode("utf-8"),
        "application/json",
        expected_offset=0,
        expected_limit=2,
    )

    assert page.items == ()
    assert page.total == 0
    assert page.has_more is False


def test_empty_later_page_ends_query_when_server_reports_a_phantom_total():
    document = json.loads(list_content())
    document["data"]["list"] = []
    document["data"]["pagenation"].update({"offset": 2, "total": 5})

    page = make_adapter().parse_list(
        json.dumps(document).encode("utf-8"),
        "application/json",
        expected_offset=2,
        expected_limit=2,
    )

    assert page.items == ()
    assert page.has_more is False


def test_live_style_thousand_yuan_salary_is_normalized_to_yuan_per_month():
    document = json.loads(list_content())
    document["data"]["list"][0].update({"lowMonthPay": 9.0, "highMonthPay": 15.0})

    page = make_adapter().parse_list(
        json.dumps(document).encode("utf-8"), "application/json"
    )

    assert page.items[0].salary == "9000-15000元/月"


def test_live_style_millisecond_publish_date_has_explicit_raw_evidence():
    adapter = make_adapter()
    document = json.loads(list_content())
    document["data"]["list"][0]["publishDate"] = 1785463112051
    item = adapter.parse_list(
        json.dumps(document).encode("utf-8"), "application/json"
    ).items[0]

    record = adapter.parse_detail(
        detail_content(), item, adapter.build_detail_url(item)
    )

    assert item.published_at == "2026-07-31"
    assert record["published_at_confidence"] == 0.9
    assert "1785463112051" in record["published_at_evidence"]
    assert "2026-07-31" in record["published_at_evidence"]


@pytest.mark.parametrize(
    ("offset", "limit"),
    [(41, 2), (40, 1), (39, 2), (0, 101), (0, 1)],
)
def test_response_pagination_cannot_cross_source_budget_or_contradict_items(
    offset, limit
):
    document = json.loads(list_content())
    document["data"]["pagenation"].update(
        {"offset": offset, "limit": limit, "total": 1000}
    )

    with pytest.raises(AdapterStructureError):
        make_adapter(max_records=40).parse_list(
            json.dumps(document).encode("utf-8"), "application/json"
        )


@pytest.mark.parametrize(
    "html",
    [
        "<html><title>普通页面</title><body>无岗位容器</body></html>",
        "<html><title>用户登录</title><body><form action='/login'><input type='password'></form></body></html>",
        "<html><title>访问验证</title><body><input id='captcha'>请输入验证码</body></html>",
        "<html><title>404 页面不存在</title><body>页面不存在</body></html>",
        "<html><title>用户登录</title><body><pre class='mainContent'>伪岗位内容</pre></body></html>",
        "<html><title>系统错误</title><body><pre class='mainContent'>伪岗位内容</pre></body></html>",
        "<html><title>岗位详情</title><body><input type='password'><pre class='mainContent'>请先登录后查看岗位</pre></body></html>",
        "<html><title>岗位详情</title><body><form action='/account/login'><pre class='mainContent'>登录后查看岗位详情</pre></form></body></html>",
        "<html><title>岗位详情</title><body><div class='captcha-box'></div><pre class='mainContent'>请输入验证码</pre></body></html>",
        "<html><title>访问验证</title><body><pre class='mainContent'>负责后端开发。</pre></body></html>",
        "<html><pre class='mainContent'></pre></html>",
        "<html><pre class='mainContent'>one</pre><pre class='mainContent'>two</pre></html>",
    ],
)
def test_parse_detail_rejects_missing_ambiguous_empty_or_access_gate_pages(html):
    adapter = make_adapter()
    item = adapter.parse_list(list_content(), "application/json").items[0]

    with pytest.raises((AdapterStructureError, AdapterRecordError)):
        adapter.parse_detail(
            html.encode("utf-8"), item, adapter.build_detail_url(item)
        )


@pytest.mark.parametrize(
    "global_login_ui",
    [
        "<a href='/login'>用户登录</a>",
        "<div class='login-modal'><input type='password'></div>",
        "<form action='/account/login'><button>登录</button></form>",
        "<div class='captcha-box'><input name='captcha'></div>",
    ],
)
def test_short_normal_detail_with_global_login_ui_is_not_blocked(global_login_ui):
    adapter = make_adapter()
    item = adapter.parse_list(list_content(), "application/json").items[0]
    html = (
        "<html><title>后端开发工程师</title><body>"
        + global_login_ui
        + "<pre class='mainContent'>负责后端开发。</pre></body></html>"
    )

    record = adapter.parse_detail(
        html.encode("utf-8"), item, adapter.build_detail_url(item)
    )

    assert record["job_description_raw"] == "负责后端开发。"


def test_job_description_with_sign_in_and_captcha_technical_terms_is_valid():
    adapter = make_adapter()
    item = adapter.parse_list(list_content(), "application/json").items[0]
    description = (
        "岗位职责：负责 sign in 流程及 CAPTCHA 模块开发，完善账号安全策略、"
        "接口测试、日志审计和异常处理，并持续维护相关服务。\n"
        "岗位要求：熟悉 Python、Web 安全基础与自动化测试。"
    )
    html = (
        "<html><title>认证平台开发工程师</title><body>"
        f"<pre class='mainContent'>{description}</pre>"
        "</body></html>"
    )

    record = adapter.parse_detail(
        html.encode("utf-8"), item, adapter.build_detail_url(item)
    )

    assert record["job_description_raw"] == description


def test_security_verification_engineer_title_is_not_an_access_gate():
    adapter = make_adapter()
    item = adapter.parse_list(list_content(), "application/json").items[0]
    html = (
        "<html><title>安全验证工程师</title><body>"
        "<pre class='mainContent'>岗位职责：负责身份安全产品开发与维护。\n"
        "岗位要求：熟悉 Python 和 Web 安全。</pre>"
        "</body></html>"
    )

    record = adapter.parse_detail(
        html.encode("utf-8"), item, adapter.build_detail_url(item)
    )

    assert record["page_title"] == "安全验证工程师"


def test_detail_url_argument_must_identify_the_same_job():
    adapter = make_adapter()
    item = adapter.parse_list(list_content(), "application/json").items[0]

    with pytest.raises(AdapterRecordError, match="does not match"):
        adapter.parse_detail(
            detail_content(),
            item,
            "https://cnu.ncss.cn/student/jobs/fixture-job-002/detail.html",
        )


def test_detail_url_argument_accepts_registry_equivalent_default_port():
    adapter = make_adapter()
    item = adapter.parse_list(list_content(), "application/json").items[0]
    equivalent_url = (
        "https://cnu.ncss.cn:443/student/jobs/fixture-job-001/detail.html"
    )

    record = adapter.parse_detail(detail_content(), item, equivalent_url)

    assert record["source_record_id"] == "fixture-job-001"


def test_adapter_requires_the_registered_fixed_source_definition():
    source = make_source()
    altered = make_source(base_url="https://mirror.example.test")
    registry = SourceRegistry([source])

    with pytest.raises(ValueError, match="registered SourceDefinition"):
        NCSSAdapter(source=altered, registry=registry)
