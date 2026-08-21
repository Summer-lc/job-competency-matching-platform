from __future__ import annotations

import html
import json
import math
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from src.job_collection.adapters.base import (
    AdapterRecordError,
    AdapterStructureError,
    ListPage,
    RequestSpec,
    SourceAdapter,
    SourceJobRecord,
)
from src.job_collection.models import SourceDefinition
from src.job_collection.source_registry import SourceRegistry


class MOHRSSAdapter(SourceAdapter):
    source_id = "mohrss_public_jobs"
    initial_list_path = "/cjobs/jobinfolist/listJobinfolistIndex"
    list_path = "/cjobs/jobinfolist/listJobinfolist"
    initial_detail_path = "/cjobs/jobinfolist/cb21/showgw"
    static_detail_prefix = "/cjobs/htmls/cb21gwPages/"
    site_page_size = 20
    max_job_id_length = 32

    query_parameters = frozenset(
        {
            "ACB241",
            "rowid",
            "AAE397",
            "textfield",
            "aab019_t",
            "aab019",
            "aab020_t",
            "aab020",
            "aab022_t",
            "aab022",
            "acb239_t",
            "acb239",
            "acb228_t",
            "acb228",
            "aac011_t",
            "aac011",
            "searchtype",
            "orderType",
            "zcType",
            "AREA",
            "AREA_name",
            "ACA111",
            "ACA111_name",
            "s_aae397",
            "s_acb241",
            "textfield",
        }
    )
    managed_parameters = frozenset(
        {"pageNo", "pagecount", "totalpages", "totalcount"}
    )
    source_date_fields = (
        "s_ctime",
        "s_uptime",
        "s_aae395",
        "s_aae396",
        "s_aae397",
        "s_aae398",
    )
    promoted_fields = frozenset(
        {
            "acb200",
            "aca112",
            "aab004",
            "aab302",
            "area_",
            "aca111_",
            "acb241",
            "acb242",
            "acb239",
            "acb239_",
            "acb239_t",
            "acb228",
            "acb228_",
            "acb228_t",
            *source_date_fields,
        }
    )

    _positive_integer = re.compile(r"^[1-9][0-9]*$")
    _nonnegative_integer = re.compile(r"^(?:0|[1-9][0-9]*)$")
    _aae_pii_key = re.compile(r"^aae00[456](?:_|$)", re.IGNORECASE)
    _exact_gate_main = re.compile(
        r"^(?:(?:请先登录|请登录)|"
        r"(?:请先登录|请登录)(?:后)?查看"
        r"(?:(?:岗位|职位)(?:详情|信息)?|详情|信息)?|"
        r"登录后查看(?:(?:岗位|职位)(?:详情|信息)?|详情|信息)|"
        r"访问受限)[。！!]*$",
        re.IGNORECASE,
    )
    _email = re.compile(
        r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])"
    )
    _mobile_phone = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
    _landline_phone = re.compile(r"(?<!\d)0\d{2,3}[- ]?\d{7,8}(?!\d)")
    _contact_line = re.compile(
        r"^\s*(?:联系人|联系电话|联系地址|通讯地址|详细地址|电子邮箱|邮箱|电话|手机|联系方式)\s*[:：]",
        re.IGNORECASE,
    )
    _contact_fragment = re.compile(
        r"(?:联系人|联系电话|联系地址|通讯地址|详细地址|电子邮箱|邮箱|电话|手机|联系方式)\s*[:：].*$",
        re.IGNORECASE,
    )
    _communication_tokens = frozenset(
        {
            "phone",
            "tel",
            "telephone",
            "mobile",
            "email",
            "mail",
            "wechat",
            "weixin",
            "qq",
            "address",
        }
    )
    _business_semantic_tokens = frozenset(
        {
            "experience",
            "skill",
            "skills",
            "knowledge",
            "system",
            "platform",
            "protocol",
            "marketing",
            "template",
            "market",
            "delivery",
            "office",
            "development",
            "technology",
        }
    )
    _role_sensitive_tokens = frozenset(
        {
            "agent",
            "applicant",
            "candidate",
            "contactname",
            "contactperson",
            "director",
            "employee",
            "handler",
            "hr",
            "leader",
            "liaison",
            "manager",
            "operator",
            "owner",
            "person",
            "recruiter",
            "representative",
        }
    )
    _business_name_tokens = frozenset(
        {
            "business",
            "category",
            "company",
            "department",
            "employer",
            "enterprise",
            "industry",
            "job",
            "occupation",
            "organisation",
            "organization",
            "position",
            "product",
            "profession",
            "project",
            "sector",
            "service",
            "skill",
            "skills",
            "technology",
            "trade",
        }
    )
    _chinese_person_role = re.compile(
        r"(?:联系人|联络人|负责人|经办人|姓名|(?:招聘|人事|hr)(?:人员|专员))",
        re.IGNORECASE,
    )
    _chinese_sensitive_tokens = frozenset(
        {
            "联系电话",
            "手机号码",
            "邮箱",
            "微信",
            "qq号",
        }
    )
    _chinese_communication_markers = frozenset(
        {"移动", "手机", "电话", "邮箱", "邮件", "微信", "qq", "地址"}
    )
    _chinese_business_markers = frozenset(
        {
            "经验",
            "技能",
            "知识",
            "系统",
            "平台",
            "协议",
            "营销",
            "模板",
            "市场",
            "交付",
            "办公",
            "开发",
            "技术",
        }
    )
    _education_codes = {
        "10": "研究生教育",
        "11": "博士研究生",
        "12": "硕士研究生",
        "20": "本科",
        "30": "大专",
        "40": "中专",
        "60": "高中",
        "70": "初中",
        "80": "小学",
        "90": "其他",
    }
    _recognized_salary_units = frozenset({"元/月", "元以上/月"})

    def __init__(self, *, source: SourceDefinition, registry: SourceRegistry) -> None:
        registered = registry.require_automatic(self.source_id)
        if source != registered:
            raise ValueError("source must equal the registered SourceDefinition")
        if source.parser_name != "mohrss" or source.collection_mode != "public_html":
            raise ValueError("registered SourceDefinition is not a MOHRSS HTML source")
        self.source = source
        self.registry = registry

    def build_bootstrap_request(self) -> RequestSpec:
        return RequestSpec(
            url=self.registry.validate_url(self.source_id, self.initial_list_path)
        )

    @staticmethod
    def validate_bootstrap(content: bytes, content_type: str | None) -> None:
        if not content_type or "html" not in content_type.lower() or not content:
            raise AdapterStructureError("MOHRSS bootstrap response is not HTML content")
        soup = BeautifulSoup(content, "html.parser")
        form = soup.select(
            "form#jobinfolistForm[action='/cjobs/jobinfolist/listJobinfolist']"
        )
        if len(form) != 1:
            raise AdapterStructureError(
                "MOHRSS bootstrap response is missing the reviewed public job form"
            )

    def build_list_request(
        self, query: str | Mapping[str, object], page_no: int, limit: int
    ) -> RequestSpec:
        self._validate_request_pagination(page_no, limit)
        if isinstance(query, str):
            query_values: Mapping[str, object] = {
                "textfield": query,
                "searchtype": "gw",
                "orderType": "score",
            }
        elif isinstance(query, Mapping):
            query_values = query
        else:
            raise ValueError("query must be a string or mapping")

        managed = set(query_values).intersection(self.managed_parameters)
        if managed:
            names = ", ".join(sorted(str(name) for name in managed))
            raise ValueError(f"MOHRSS pagination parameter(s) are managed internally: {names}")
        unknown = set(query_values).difference(self.query_parameters)
        if unknown:
            names = ", ".join(sorted(str(name) for name in unknown))
            raise ValueError(f"unknown MOHRSS query parameter(s): {names}")

        params: dict[str, str | int | float | bool] = {}
        for name, value in query_values.items():
            if value is None:
                continue
            if not isinstance(value, (str, int, float, bool)):
                raise ValueError(f"MOHRSS query parameter {name} must be scalar")
            params[name] = value
        params["pageNo"] = page_no
        url = self.registry.validate_url(self.source_id, self.list_path)
        return RequestSpec(url=url, params=params)

    def parse_list(
        self,
        content: bytes,
        content_type: str | None,
        expected_page_no: int | None = None,
        expected_limit: int | None = None,
    ) -> ListPage:
        if not content_type or "html" not in content_type.lower():
            raise AdapterStructureError("MOHRSS list response is not HTML content")
        if not content:
            raise AdapterStructureError("MOHRSS list response is empty")
        try:
            soup = BeautifulSoup(content, "html.parser")
        except Exception as exc:
            raise AdapterStructureError("MOHRSS list response is invalid HTML") from exc

        page_no = self._hidden_positive_integer(soup, "pageNo")
        page_count = self._hidden_nonnegative_integer(soup, "pagecount")
        total_pages = self._hidden_nonnegative_integer(soup, "totalpages")
        server_total = self._hidden_nonnegative_integer(soup, "totalcount")
        current_contract = page_count == total_pages and (
            server_total > 0 or page_count == 0
        )
        page_size = self.site_page_size if current_contract else page_count
        self._validate_response_pagination(
            page_no,
            page_size,
            total_pages,
            server_total,
            expected_page_no,
            expected_limit,
            current_contract=current_contract,
        )

        containers = soup.select(
            "input[type='hidden'][id='findjoblist'][name='findjoblist']"
        )
        if len(containers) != 1:
            raise AdapterStructureError(
                "MOHRSS list response must contain exactly one findjoblist field"
            )
        encoded_records = containers[0].get("value")
        if not isinstance(encoded_records, str):
            raise AdapterStructureError("MOHRSS findjoblist is missing its value")
        try:
            records = json.loads(
                html.unescape(encoded_records), parse_constant=self._reject_json_constant
            )
        except (ValueError, UnicodeDecodeError, TypeError) as exc:
            raise AdapterStructureError(
                "MOHRSS findjoblist contains invalid JSON"
            ) from exc
        if not isinstance(records, list):
            raise AdapterStructureError("MOHRSS findjoblist JSON must be an array")
        if len(records) > page_size:
            raise AdapterStructureError(
                "MOHRSS pagecount is smaller than the findjoblist array"
            )

        items = tuple(
            self._parse_list_item(value, index) for index, value in enumerate(records)
        )
        offset = (page_no - 1) * page_size
        seen = offset + len(items)
        collection_cap = min(
            self.source.max_pages * page_size,
            (self.source.max_records // page_size) * page_size,
        )
        if seen > server_total:
            raise AdapterStructureError(
                "MOHRSS findjoblist exceeds the server total or last-page remaining count"
            )
        if seen > collection_cap:
            raise AdapterStructureError(
                "MOHRSS findjoblist crosses the effective collection cap"
            )
        capped_total = min(server_total, collection_cap)
        total = capped_total
        last_allowed_page = math.ceil(total / page_size)
        has_more = seen < total and page_no < last_allowed_page
        if not items and offset < capped_total:
            raise AdapterStructureError(
                "MOHRSS returned an empty page before the capped total"
            )
        return ListPage(
            items=items,
            total=total,
            offset=offset,
            limit=page_size,
            has_more=has_more,
        )

    def build_detail_url(self, item: SourceJobRecord) -> str:
        job_id = self._validated_item_job_id(item)
        return self.registry.validate_url(
            self.source_id, f"{self.initial_detail_path}?id={job_id}"
        )

    def static_detail_url(self, item: SourceJobRecord) -> str:
        job_id = self._validated_item_job_id(item)
        return self.registry.validate_url(
            self.source_id, f"{self.static_detail_prefix}{job_id}.html"
        )

    def validate_detail_url(self, item: SourceJobRecord, url: str) -> str:
        scoped_url = self.registry.validate_url(self.source_id, url)
        expected_job_id = self._validated_item_job_id(item)
        parsed = urlsplit(scoped_url)
        if parsed.fragment:
            raise AdapterRecordError("MOHRSS detail URL must not contain a fragment")

        if parsed.path == self.initial_detail_path:
            try:
                query = parse_qs(
                    parsed.query, keep_blank_values=True, strict_parsing=True
                )
            except ValueError as exc:
                raise AdapterRecordError(
                    "MOHRSS initial detail URL has invalid query"
                ) from exc
            if set(query) != {"id"} or len(query["id"]) != 1:
                raise AdapterRecordError("MOHRSS initial detail URL has invalid query")
            actual_job_id = query["id"][0]
        elif parsed.path.startswith(self.static_detail_prefix):
            if parsed.query:
                raise AdapterRecordError("MOHRSS static detail URL must not have a query")
            suffix = parsed.path[len(self.static_detail_prefix) :]
            if not suffix.endswith(".html") or "/" in suffix:
                raise AdapterRecordError("MOHRSS static detail URL has invalid shape")
            actual_job_id = suffix[: -len(".html")]
        else:
            raise AdapterRecordError("MOHRSS detail URL has an unreviewed path")

        self._validate_job_id(actual_job_id, AdapterRecordError)
        if actual_job_id != expected_job_id:
            raise AdapterRecordError("MOHRSS detail URL does not match the list item")
        return scoped_url

    def validate_detail_redirect(
        self, item: SourceJobRecord, current_url: str, canonical_target: str
    ) -> None:
        current = self.validate_detail_url(item, current_url)
        if urlsplit(current).path != self.initial_detail_path:
            raise AdapterRecordError(
                "MOHRSS redirect must go from the initial detail URL to the static detail URL"
            )
        parsed_target = urlsplit(canonical_target)
        if not parsed_target.scheme or not parsed_target.netloc:
            raise AdapterRecordError("MOHRSS redirect target must be canonical and absolute")
        validated_target = self.validate_detail_url(item, canonical_target)
        if not urlsplit(validated_target).path.startswith(self.static_detail_prefix):
            raise AdapterRecordError(
                "MOHRSS redirect must go from the initial detail URL to the static detail URL"
            )

    def parse_detail(
        self, content: bytes, item: SourceJobRecord, url: str
    ) -> dict[str, object]:
        source_url = self.validate_detail_url(item, url)
        if not content:
            raise AdapterRecordError("MOHRSS detail response is empty")
        try:
            soup = BeautifulSoup(content, "html.parser")
        except Exception as exc:
            raise AdapterStructureError("MOHRSS detail response is invalid HTML") from exc

        page_title = self._page_title(soup)
        self._validate_detail_page(soup, page_title)
        containers = soup.select(".gwmsDiv #gwms")
        if len(containers) != 1:
            raise AdapterStructureError(
                "MOHRSS detail response must contain exactly one JD container"
            )
        container = containers[0]
        for line_break in container.find_all("br"):
            line_break.replace_with("\n")
        description = container.get_text(separator="", strip=False)
        description = description.replace("\r\n", "\n").replace("\r", "\n")
        description = description.replace("\xa0", " ").strip(" \t\n")
        self._reject_job_description_gate(container, description)
        description, pii_removed = self._sanitize_description(description)
        if not description:
            raise AdapterRecordError("MOHRSS detail JD container is empty")

        extra = self._adapter_extra(item.raw)
        if pii_removed:
            issues = list(extra.get("validation_issues", []))
            issues.append(
                {
                    "field": "job_description_raw",
                    "code": "pii_contact_text_removed",
                }
            )
            extra["validation_issues"] = issues

        return {
            "source_id": self.source_id,
            "source_record_id": item.source_record_id,
            "job_title": item.job_title,
            "company_name": item.company_name,
            "region": item.region,
            "industry": item.industry,
            "salary": item.salary or self._detail_salary(soup),
            "education": item.education,
            "experience": self._experience(soup),
            "published_at": None,
            "published_at_evidence": None,
            "published_at_confidence": 0.0,
            "confidence": 0.0,
            "job_description_raw": description,
            "page_title": page_title,
            "response_status": 200,
            "source_url": source_url,
            "adapter_extra": extra,
        }

    def _validate_request_pagination(self, page_no: int, limit: int) -> None:
        if type(page_no) is not int or page_no < 1:
            raise ValueError("page_no must be a positive integer")
        if page_no > self.source.max_pages:
            raise ValueError("page_no cannot exceed source max_pages")
        if type(limit) is not int or not 1 <= limit <= self.site_page_size:
            raise ValueError(
                f"limit must be between 1 and {self.site_page_size}"
            )
        offset = (page_no - 1) * limit
        if offset + limit > self.source.max_records:
            raise ValueError("requested page cannot cross source max_records")

    def _validate_response_pagination(
        self,
        page_no: int,
        page_size: int,
        total_pages: int,
        server_total: int,
        expected_page_no: int | None,
        expected_limit: int | None,
        *,
        current_contract: bool = False,
    ) -> None:
        if not 1 <= page_size <= self.site_page_size:
            raise AdapterStructureError(
                "MOHRSS pagination pagecount exceeds the reviewed bound"
            )
        calculated_pages = math.ceil(server_total / page_size)
        if server_total == 0:
            if page_no != 1 or total_pages not in {0, 1}:
                raise AdapterStructureError(
                    "MOHRSS pagination has a contradictory zero-result page"
                )
        elif total_pages != calculated_pages:
            raise AdapterStructureError(
                "MOHRSS pagination totalpages contradicts totalcount"
            )
        if server_total > 0 and page_no > total_pages:
            raise AdapterStructureError("MOHRSS pagination pageNo exceeds totalpages")
        if page_no > self.source.max_pages:
            raise AdapterStructureError("MOHRSS pagination pageNo exceeds source max_pages")
        offset = (page_no - 1) * page_size
        if offset + page_size > self.source.max_records:
            raise AdapterStructureError("MOHRSS pagination crosses source max_records")

        if expected_page_no is not None:
            if type(expected_page_no) is not int or expected_page_no < 1:
                raise ValueError("expected_page_no must be a positive integer")
            if expected_page_no > self.source.max_pages:
                raise ValueError("expected_page_no cannot exceed source max_pages")
            if page_no != expected_page_no:
                raise AdapterStructureError(
                    "MOHRSS response does not match the requested page"
                )
        if expected_limit is not None:
            if (
                type(expected_limit) is not int
                or not 1 <= expected_limit <= self.site_page_size
            ):
                raise ValueError(
                    f"expected_limit must be between 1 and {self.site_page_size}"
                )
            if not current_contract and page_size != expected_limit:
                raise AdapterStructureError(
                    "MOHRSS pagecount does not match the requested limit"
                )

    def _parse_list_item(self, value: object, index: int) -> SourceJobRecord:
        if not isinstance(value, dict):
            raise AdapterStructureError(f"MOHRSS findjoblist[{index}] must be an object")
        if "acb200" not in value:
            raise AdapterStructureError(
                f"MOHRSS findjoblist[{index}] is missing acb200; list primary-key contract changed"
            )
        try:
            job_id = self._validate_job_id(value["acb200"], AdapterStructureError)
        except AdapterStructureError as exc:
            raise AdapterStructureError(
                f"MOHRSS findjoblist[{index}] has invalid acb200"
            ) from exc

        pii_filter_findings: list[dict[str, str]] = []
        safe_raw = self._sanitize_mapping(value, "", pii_filter_findings)
        if pii_filter_findings:
            safe_raw["pii_filter_findings"] = pii_filter_findings
        salary, _ = self._validated_salary(
            safe_raw.get("acb241"),
            safe_raw.get("acb242"),
            safe_raw.get("acb239_")
            or safe_raw.get("acb239_t")
            or safe_raw.get("acb239"),
        )
        education = self._optional_text(
            safe_raw.get("acb228_") or safe_raw.get("acb228_t")
        )
        if education is None:
            education_code = self._optional_text(safe_raw.get("acb228"))
            education = self._education_codes.get(education_code, education_code)
        return SourceJobRecord(
            source_record_id=job_id,
            job_title=self._optional_text(safe_raw.get("aca112")),
            company_name=self._optional_text(safe_raw.get("aab004")),
            region=self._optional_text(
                safe_raw.get("aab302") or safe_raw.get("area_")
            ),
            industry=self._optional_text(safe_raw.get("aca111_")),
            salary=salary,
            education=education,
            published_at=None,
            raw=safe_raw,
        )

    @classmethod
    def _hidden_positive_integer(cls, soup: BeautifulSoup, name: str) -> int:
        nodes = soup.select(
            f"input[type='hidden'][id='{name}'][name='{name}']"
        )
        if len(nodes) != 1:
            raise AdapterStructureError(
                f"MOHRSS pagination must contain exactly one {name} field"
            )
        value = nodes[0].get("value")
        if not isinstance(value, str) or cls._positive_integer.fullmatch(value) is None:
            raise AdapterStructureError(
                f"MOHRSS pagination {name} must be a strict positive integer"
            )
        return int(value)

    @classmethod
    def _hidden_nonnegative_integer(cls, soup: BeautifulSoup, name: str) -> int:
        nodes = soup.select(
            f"input[type='hidden'][id='{name}'][name='{name}']"
        )
        if len(nodes) != 1:
            raise AdapterStructureError(
                f"MOHRSS pagination must contain exactly one {name} field"
            )
        value = nodes[0].get("value")
        if (
            not isinstance(value, str)
            or cls._nonnegative_integer.fullmatch(value) is None
        ):
            raise AdapterStructureError(
                f"MOHRSS pagination {name} must be a strict non-negative integer"
            )
        return int(value)

    @classmethod
    def _validate_job_id(cls, value: object, error_type: type[Exception]) -> str:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise error_type("MOHRSS acb200 must be a positive numeric identifier")
        job_id = str(value).strip()
        if (
            len(job_id) > cls.max_job_id_length
            or cls._positive_integer.fullmatch(job_id) is None
        ):
            raise error_type("MOHRSS acb200 must be a positive numeric identifier")
        return job_id

    @classmethod
    def _validated_item_job_id(cls, item: SourceJobRecord) -> str:
        return cls._validate_job_id(item.source_record_id, AdapterRecordError)

    @staticmethod
    def _reject_json_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant is not allowed: {value}")

    @classmethod
    def _validated_salary(
        cls, low: object, high: object, unit: object
    ) -> tuple[str | None, str | None]:
        low_number, low_valid = cls._salary_number(low)
        high_number, high_valid = cls._salary_number(high)
        if not low_valid or not high_valid:
            return None, "salary_value_must_be_a_finite_non_negative_number"
        if low_number is not None and high_number is not None and low_number > high_number:
            return None, "salary_low_exceeds_high"
        if low_number is None and high_number is None:
            return None, None

        unit_text = cls._optional_text(unit)
        if unit_text not in cls._recognized_salary_units:
            return None, "salary_unit_unrecognized"
        unit_text, _ = cls._redact_text(unit_text)
        if low_number is not None and high_number is not None:
            low_text = cls._format_salary_number(low_number)
            high_text = cls._format_salary_number(high_number)
            if low_number == high_number:
                return f"{low_text}{unit_text}", None
            return f"{low_text}-{high_text}{unit_text}", None
        if low_number is not None:
            return f"最低{cls._format_salary_number(low_number)}{unit_text}", None
        return f"最高{cls._format_salary_number(high_number)}{unit_text}", None

    @staticmethod
    def _salary_number(value: object) -> tuple[Decimal | None, bool]:
        if value is None or value == "":
            return None, True
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            return None, False
        if isinstance(value, str):
            candidate = value.strip()
            if not candidate or re.fullmatch(r"(?:0|[1-9]\d*)(?:\.\d+)?", candidate) is None:
                return None, False
        else:
            candidate = str(value)
        try:
            number = Decimal(candidate)
        except InvalidOperation:
            return None, False
        if not number.is_finite() or number < 0:
            return None, False
        return number, True

    @staticmethod
    def _format_salary_number(value: Decimal) -> str:
        if value == value.to_integral():
            return str(int(value))
        return format(value.normalize(), "f")

    @staticmethod
    def _optional_text(value: object) -> str | None:
        if value is None or isinstance(value, (dict, list, tuple, set)):
            return None
        text = str(value).strip()
        return text or None

    @classmethod
    def _page_title(cls, soup: BeautifulSoup) -> str | None:
        if soup.title is None:
            return None
        title = " ".join(soup.title.get_text(" ", strip=True).split())
        return title or None

    @classmethod
    def _validate_detail_page(
        cls, soup: BeautifulSoup, page_title: str | None
    ) -> None:
        if page_title != "中国公共招聘网_招聘岗位":
            raise AdapterStructureError("MOHRSS returned a wrong or access-gate page")

    @classmethod
    def _reject_job_description_gate(cls, container: Any, description: str) -> None:
        normalized = " ".join(description.split()).lower()
        structural_gate_markers = (
            "请输入验证码",
            "访问验证",
            "verify you are human",
            "sign in to continue",
            "log in to continue",
        )
        job_markers = (
            "岗位职责",
            "岗位要求",
            "任职要求",
            "工作内容",
            "负责",
            "熟悉",
        )
        gate_scope = container.find_parent(class_="gwmsDiv") or container
        supporting_gate_structure = (
            gate_scope.select_one(
                "input[type='password'], "
                "[id*='captcha' i], [class*='captcha' i], [name*='captcha' i]"
            )
            is not None
            or any(
                "login" in str(form.get("action", "")).lower()
                or "signin" in str(form.get("action", "")).lower()
                for form in gate_scope.select("form")
            )
        )
        short_non_job_main = len(normalized) <= 200 and not any(
            marker in normalized for marker in job_markers
        )
        exact_gate_main = cls._exact_gate_main.fullmatch(normalized) is not None
        structural_gate_main = any(
            marker in normalized for marker in structural_gate_markers
        )
        if short_non_job_main and (
            exact_gate_main
            or (supporting_gate_structure and structural_gate_main)
        ):
            raise AdapterStructureError("MOHRSS returned a login or verification page")
        error_markers = (
            "页面不存在",
            "系统错误",
            "服务异常",
            "访问被拒绝",
            "not found",
            "forbidden",
        )
        if (
            len(normalized) <= 200
            and not any(marker in normalized for marker in job_markers)
            and any(marker in normalized for marker in error_markers)
        ):
            raise AdapterStructureError("MOHRSS returned an error page")

    @classmethod
    def _sanitize_description(cls, description: str) -> tuple[str, bool]:
        kept_lines: list[str] = []
        removed = False
        for line in description.split("\n"):
            if cls._contact_line.match(line):
                removed = True
                continue
            sanitized, changed = cls._redact_text(line)
            removed = removed or changed
            kept_lines.append(sanitized)
        result = "\n".join(kept_lines).strip(" \t\n")
        result = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", result)
        return result, removed

    @classmethod
    def _redact_text(cls, value: str) -> tuple[str, bool]:
        result, contact_count = cls._contact_fragment.subn("[REDACTED]", value)
        changed = contact_count > 0
        for pattern in (cls._email, cls._mobile_phone, cls._landline_phone):
            result, count = pattern.subn("[REDACTED]", result)
            if count:
                changed = True
        return result, changed

    @classmethod
    def _key_tokens(cls, key: str) -> tuple[str, ...]:
        acronym_split = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", key.strip())
        camel_split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", acronym_split)
        normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", camel_split)
        return tuple(token.casefold() for token in normalized.split("_") if token)

    @classmethod
    def _pii_key_action(
        cls, key: str, *, isolate_unknown_name: bool
    ) -> tuple[str, str | None]:
        normalized = key.strip().casefold()
        if cls._aae_pii_key.match(normalized) is not None:
            return "drop", "mohrss_contact_field_code"
        if normalized == "pii_filter_findings":
            return "drop", "reserved_adapter_field"

        tokens = cls._key_tokens(key)
        token_set = set(tokens)

        if token_set.intersection(cls._role_sensitive_tokens):
            return "finding", "role_contact_key"
        if "contact" in token_set and token_set.intersection({"person", "name"}):
            return "finding", "role_contact_key"
        if "recruiter" in token_set and "name" in token_set:
            return "finding", "role_contact_key"
        if cls._chinese_person_role.search(normalized) is not None:
            return "finding", "chinese_role_contact_key"
        if isolate_unknown_name and "name" in token_set:
            business_name_tokens = (
                cls._business_name_tokens | cls._business_semantic_tokens
            )
            if token_set.intersection(business_name_tokens):
                return "keep", None
            return "finding", "unknown_name_key"
        if any(token in cls._chinese_sensitive_tokens for token in tokens):
            return "finding", "chinese_communication_identifier_key"

        communication_tokens = token_set.intersection(cls._communication_tokens)
        if communication_tokens:
            if token_set.intersection(cls._business_semantic_tokens):
                return "keep", None
            return "finding", "communication_identifier_key"

        has_chinese_communication = any(
            marker in normalized for marker in cls._chinese_communication_markers
        )
        if has_chinese_communication:
            has_business_semantics = any(
                marker in normalized for marker in cls._chinese_business_markers
            )
            if has_business_semantics:
                return "keep", None
            return "finding", "chinese_communication_identifier_key"

        if "contact" in token_set:
            return "finding", "unrecognized_contact_key"
        if "personal" in token_set and token_set.intersection(
            {"data", "details", "info", "identity"}
        ):
            return "finding", "unrecognized_personal_data_key"
        if normalized in {"个人信息", "身份信息"}:
            return "finding", "unrecognized_personal_data_key"
        return "keep", None

    @classmethod
    def _sanitize_mapping(
        cls,
        raw: Mapping[object, object],
        path: str,
        findings: list[dict[str, str]],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for raw_key, value in raw.items():
            key = str(raw_key)
            key_path = f"{path}.{key}" if path else key
            known_top_level_field = not path and key in (
                cls.promoted_fields | cls.query_parameters | cls.managed_parameters
            )
            action, reason = cls._pii_key_action(
                key, isolate_unknown_name=not known_top_level_field
            )
            if action == "drop":
                continue
            if action == "finding":
                findings.append({"path": key_path, "reason": str(reason)})
                continue
            result[key] = cls._sanitize_value(value, key_path, findings)
        return result

    @classmethod
    def _sanitize_value(
        cls, value: object, path: str, findings: list[dict[str, str]]
    ) -> Any:
        if isinstance(value, Mapping):
            return cls._sanitize_mapping(value, path, findings)
        if isinstance(value, list):
            return [
                cls._sanitize_value(item, f"{path}[{index}]", findings)
                for index, item in enumerate(value)
            ]
        if isinstance(value, str):
            sanitized, _ = cls._redact_text(value)
            return sanitized
        return value

    @classmethod
    def _adapter_extra(cls, raw: dict[str, Any]) -> dict[str, object]:
        extra: dict[str, object] = {
            key: value for key, value in raw.items() if key not in cls.promoted_fields
        }
        source_dates = {
            key: raw[key]
            for key in cls.source_date_fields
            if cls._optional_text(raw.get(key)) is not None
        }
        if source_dates:
            extra["source_dates"] = source_dates

        validation_issues: list[dict[str, object]] = []
        _, salary_issue = cls._validated_salary(
            raw.get("acb241"),
            raw.get("acb242"),
            raw.get("acb239_") or raw.get("acb239_t") or raw.get("acb239"),
        )
        if salary_issue:
            validation_issues.append(
                {
                    "field": "salary",
                    "code": salary_issue,
                    "raw": {
                        "acb241": raw.get("acb241"),
                        "acb242": raw.get("acb242"),
                        "unit": {
                            "acb239_": raw.get("acb239_"),
                            "acb239_t": raw.get("acb239_t"),
                            "acb239": raw.get("acb239"),
                        },
                    },
                }
            )
        if validation_issues:
            extra["validation_issues"] = validation_issues
        return extra

    @classmethod
    def _experience(cls, soup: BeautifulSoup) -> str | None:
        for label in soup.find_all(string=re.compile(r"^(?:工作经验|经验要求)\s*$")):
            parent = label.parent
            if parent is None or parent.parent is None:
                continue
            values = [
                " ".join(node.get_text(" ", strip=True).split())
                for node in parent.parent.find_all(recursive=False)
                if node is not parent
            ]
            for value in values:
                if value:
                    sanitized, _ = cls._redact_text(value)
                    return sanitized or None
        return None

    @classmethod
    def _detail_salary(cls, soup: BeautifulSoup) -> str | None:
        visible_text = " ".join(soup.get_text(" ", strip=True).split())
        match = re.search(
            r"(?:薪资待遇|工资待遇|薪资标准)\s*[:：]?\s*"
            r"(0|[1-9]\d*)(?:\.0+)?\s*(元以上/月)",
            visible_text,
        )
        if match is None:
            return None
        return f"{match.group(1)}{match.group(2)}"
