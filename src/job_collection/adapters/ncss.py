from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, unquote, urlsplit

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


class NCSSAdapter(SourceAdapter):
    source_id = "ncss_public_jobs"
    list_path = "/student/jobs/jobslist/ajax/"
    max_page_size = 100

    query_parameters = frozenset(
        {
            "jobType",
            "areaCode",
            "jobName",
            "monthPay",
            "industrySectors",
            "recruitType",
            "property",
            "categoryCode",
            "memberLevel",
            "keyUnits",
            "degreeCode",
            "sourcesName",
            "sourcesType",
        }
    )

    def __init__(self, *, source: SourceDefinition, registry: SourceRegistry) -> None:
        registered = registry.require_automatic(self.source_id)
        if source != registered:
            raise ValueError("source must equal the registered SourceDefinition")
        if source.parser_name != "ncss" or source.collection_mode != "public_json":
            raise ValueError("registered SourceDefinition is not an NCSS JSON source")
        self.source = source
        self.registry = registry

    def build_list_request(
        self, query: str | Mapping[str, object], offset: int, limit: int
    ) -> RequestSpec:
        self._validate_page_bounds(offset, limit)
        if isinstance(query, str):
            query_values: Mapping[str, object] = {"jobName": query}
        elif isinstance(query, Mapping):
            query_values = query
        else:
            raise ValueError("query must be a string or mapping")

        unknown = set(query_values).difference(self.query_parameters)
        if unknown:
            names = ", ".join(sorted(str(name) for name in unknown))
            raise ValueError(f"unknown NCSS query parameter(s): {names}")

        params: dict[str, str | int | float | bool] = {}
        for name, value in query_values.items():
            if value is None:
                continue
            if not isinstance(value, (str, int, float, bool)):
                raise ValueError(f"NCSS query parameter {name} must be scalar")
            params[name] = value
        params.update({"offset": offset, "limit": limit})
        url = self.registry.validate_url(self.source_id, self.list_path)
        return RequestSpec(url=url, params=params)

    def parse_list(
        self,
        content: bytes,
        content_type: str | None,
        expected_offset: int | None = None,
        expected_limit: int | None = None,
    ) -> ListPage:
        if not content_type or "json" not in content_type.lower():
            raise AdapterStructureError("NCSS list response is not JSON content")
        try:
            document = json.loads(content, parse_constant=self._reject_json_constant)
        except (ValueError, UnicodeDecodeError, TypeError) as exc:
            raise AdapterStructureError("NCSS list response contains invalid JSON") from exc
        if not isinstance(document, dict):
            raise AdapterStructureError("NCSS list response root must be an object")

        data = document.get("data")
        if not isinstance(data, dict):
            raise AdapterStructureError("NCSS list response is missing data object")
        records = data.get("list")
        if not isinstance(records, list):
            raise AdapterStructureError("NCSS list response data.list must be an array")
        pagination = data.get("pagenation")
        if not isinstance(pagination, dict):
            raise AdapterStructureError(
                "NCSS list response data.pagenation must be an object"
            )

        items = tuple(self._parse_list_item(value, index) for index, value in enumerate(records))
        server_total, offset, limit = self._parse_pagination(pagination)
        self._validate_response_pagination(offset, limit, len(items))
        self._validate_expected_pagination(
            offset, limit, expected_offset, expected_limit
        )
        seen = offset + len(items)
        total = max(seen, min(server_total, self.source.max_records))
        has_more = len(items) == limit and seen < total
        if not items:
            total = offset
        return ListPage(
            items=items,
            total=total,
            offset=offset,
            limit=limit,
            has_more=has_more,
        )

    def build_detail_url(self, item: SourceJobRecord) -> str:
        job_id = item.source_record_id.strip()
        if (
            not job_id
            or job_id in {".", ".."}
            or "/" in job_id
            or "\\" in job_id
            or any(ord(character) < 32 or ord(character) == 127 for character in job_id)
        ):
            raise AdapterRecordError("NCSS jobId is empty or unsafe")
        encoded_job_id = quote(job_id, safe="")
        path = f"/student/jobs/{encoded_job_id}/detail.html"
        return self.registry.validate_url(self.source_id, path)

    def parse_detail(
        self, content: bytes, item: SourceJobRecord, url: str
    ) -> dict[str, object]:
        source_url = self.registry.validate_url(self.source_id, url)
        expected_url = self.build_detail_url(item)
        if self._canonical_url(source_url) != self._canonical_url(expected_url):
            raise AdapterRecordError("NCSS detail URL does not match the list item")
        if not content:
            raise AdapterRecordError("NCSS detail response is empty")
        try:
            soup = BeautifulSoup(content, "html.parser")
        except Exception as exc:
            raise AdapterStructureError("NCSS detail response is invalid HTML") from exc

        page_title = self._page_title(soup)
        self._reject_page_title(soup, page_title)
        containers = soup.select("pre.mainContent")
        if len(containers) != 1:
            raise AdapterStructureError(
                "NCSS detail response must contain exactly one JD container"
            )
        container = containers[0]
        for line_break in container.find_all("br"):
            line_break.replace_with("\n")
        description = container.get_text(separator="", strip=False)
        description = description.replace("\r\n", "\n").replace("\r", "\n")
        description = description.replace("\xa0", " ").strip(" \t\n")
        self._reject_job_description_gate(soup, description, page_title)
        if not description:
            raise AdapterRecordError("NCSS detail JD container is empty")

        published_at = item.published_at
        raw_publish_date = item.raw.get("publishDate")
        evidence = None
        if published_at:
            evidence = f"NCSS列表字段 publishDate: {raw_publish_date}"
            if str(raw_publish_date) != published_at:
                evidence += f" -> {published_at}"
        confidence = 0.9 if published_at else 0.0
        industry = item.industry or self._single_detail_text(soup, "#mainindustries")
        return {
            "source_id": self.source_id,
            "source_record_id": item.source_record_id,
            "job_title": item.job_title,
            "company_name": item.company_name,
            "region": item.region,
            "industry": industry,
            "salary": item.salary,
            "education": item.education,
            "experience": self._experience(soup),
            "published_at": published_at,
            "published_at_evidence": evidence,
            "published_at_confidence": confidence,
            "confidence": confidence,
            "job_description_raw": description,
            "page_title": page_title,
            "response_status": 200,
            "source_url": source_url,
            "adapter_extra": self._adapter_extra(item.raw),
        }

    def _validate_page_bounds(self, offset: int, limit: int) -> None:
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        maximum = min(self.max_page_size, self.source.max_records)
        if type(limit) is not int or not 1 <= limit <= maximum:
            raise ValueError(f"limit must be between 1 and {maximum}")
        if offset >= self.source.max_records:
            raise ValueError("offset must be less than source max_records")
        if offset + limit > self.source.max_records:
            raise ValueError("offset + limit cannot exceed source max_records")

    def _validate_response_pagination(
        self, offset: int, limit: int, item_count: int
    ) -> None:
        maximum = min(self.max_page_size, self.source.max_records)
        if limit > maximum:
            raise AdapterStructureError(
                f"NCSS pagination limit exceeds the allowed page size {maximum}"
            )
        if offset >= self.source.max_records:
            raise AdapterStructureError(
                "NCSS pagination offset reaches or exceeds source max_records"
            )
        if offset + limit > self.source.max_records:
            raise AdapterStructureError(
                "NCSS pagination crosses source max_records"
            )
        if item_count > limit:
            raise AdapterStructureError(
                "NCSS pagination limit is smaller than data.list"
            )

    def _validate_expected_pagination(
        self,
        offset: int,
        limit: int,
        expected_offset: int | None,
        expected_limit: int | None,
    ) -> None:
        if expected_offset is not None:
            if type(expected_offset) is not int or expected_offset < 0:
                raise ValueError("expected_offset must be a non-negative integer")
            if expected_offset >= self.source.max_records:
                raise ValueError("expected_offset must be less than source max_records")
            if offset != expected_offset:
                raise AdapterStructureError(
                    "NCSS pagination offset does not match the requested offset"
                )
        if expected_limit is not None:
            maximum = min(self.max_page_size, self.source.max_records)
            if type(expected_limit) is not int or not 1 <= expected_limit <= maximum:
                raise ValueError(f"expected_limit must be between 1 and {maximum}")
            if limit != expected_limit:
                raise AdapterStructureError(
                    "NCSS pagination limit does not match the requested limit"
                )
        if (
            expected_offset is not None
            and expected_limit is not None
            and expected_offset + expected_limit > self.source.max_records
        ):
            raise ValueError(
                "expected_offset + expected_limit cannot exceed source max_records"
            )

    def _parse_list_item(self, value: object, index: int) -> SourceJobRecord:
        if not isinstance(value, dict):
            raise AdapterStructureError(f"NCSS data.list[{index}] must be an object")
        raw_job_id = value.get("jobId")
        # jobId is the pagination identity; absence means the list contract changed.
        if isinstance(raw_job_id, bool) or not isinstance(raw_job_id, (str, int)):
            raise AdapterStructureError(f"NCSS data.list[{index}] is missing jobId")
        job_id = str(raw_job_id).strip()
        if not job_id:
            raise AdapterStructureError(f"NCSS data.list[{index}] has empty jobId")
        salary, _ = self._validated_salary(
            value.get("lowMonthPay"), value.get("highMonthPay")
        )
        published_at, _ = self._validated_publish_date(value.get("publishDate"))
        return SourceJobRecord(
            source_record_id=job_id,
            job_title=self._optional_text(value.get("jobName")),
            company_name=self._optional_text(value.get("recName")),
            region=self._optional_text(value.get("areaCodeName")),
            industry=self._optional_text(
                value.get("industrySectorsName", value.get("industryName"))
            ),
            salary=salary,
            education=self._optional_text(value.get("degreeName")),
            published_at=published_at,
            raw=dict(value),
        )

    def _parse_pagination(self, value: dict[str, Any]) -> tuple[int, int, int]:
        total = self._pagination_integer(value, ("total", "totalCount", "records"), 0)
        limit = self._pagination_integer(
            value, ("limit", "pageSize", "page_size", "size"), 1
        )
        if "offset" in value:
            offset = self._strict_nonnegative_integer(value["offset"], "offset")
        else:
            page_number = self._pagination_integer(
                value,
                ("pageNo", "pageNum", "currentPage", "pageIndex", "current"),
                1,
            )
            if page_number < 1:
                raise AdapterStructureError("NCSS pagination page number must be positive")
            offset = (page_number - 1) * limit
        return total, offset, limit

    def _pagination_integer(
        self, value: dict[str, Any], names: tuple[str, ...], minimum: int
    ) -> int:
        for name in names:
            if name in value:
                number = self._strict_nonnegative_integer(value[name], name)
                if number < minimum:
                    raise AdapterStructureError(
                        f"NCSS pagination {name} must be at least {minimum}"
                    )
                return number
        raise AdapterStructureError(
            f"NCSS pagination is missing one of: {', '.join(names)}"
        )

    @staticmethod
    def _strict_nonnegative_integer(value: object, name: str) -> int:
        if type(value) is not int or value < 0:
            raise AdapterStructureError(
                f"NCSS pagination {name} must be a non-negative integer"
            )
        return value

    @staticmethod
    def _reject_json_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant is not allowed: {value}")

    @staticmethod
    def _canonical_url(url: str) -> tuple[str, str, int, str, str, str]:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        port = parsed.port or (443 if scheme == "https" else 80)
        path = unquote(parsed.path, errors="strict")
        return (
            scheme,
            (parsed.hostname or "").lower(),
            port,
            path,
            parsed.query,
            parsed.fragment,
        )

    @staticmethod
    def _optional_text(value: object) -> str | None:
        if value is None or isinstance(value, (dict, list, tuple, set)):
            return None
        text = str(value).strip()
        return text or None

    @classmethod
    def _validated_salary(
        cls, low: object, high: object
    ) -> tuple[str | None, str | None]:
        low_number, low_valid = cls._salary_number(low)
        high_number, high_valid = cls._salary_number(high)
        if not low_valid or not high_valid:
            return None, "salary_value_must_be_a_finite_non_negative_number"
        if (
            low_number is not None
            and high_number is not None
            and low_number > high_number
        ):
            return None, "salary_low_exceeds_high"
        if low_number is not None and high_number is not None:
            low_number = cls._salary_yuan_amount(low_number)
            high_number = cls._salary_yuan_amount(high_number)
            low_text = cls._format_salary_number(low_number)
            high_text = cls._format_salary_number(high_number)
            if low_number == high_number:
                return f"{low_text}元/月", None
            return f"{low_text}-{high_text}元/月", None
        if low_number is not None:
            low_number = cls._salary_yuan_amount(low_number)
            return f"最低{cls._format_salary_number(low_number)}元/月", None
        if high_number is not None:
            high_number = cls._salary_yuan_amount(high_number)
            return f"最高{cls._format_salary_number(high_number)}元/月", None
        return None, None

    @staticmethod
    def _salary_yuan_amount(value: int | float) -> int | float:
        return value * 1000 if 0 < value < 1000 else value

    @staticmethod
    def _salary_number(value: object) -> tuple[int | float | None, bool]:
        if value is None:
            return None, True
        if type(value) is int:
            return (value, True) if value >= 0 else (None, False)
        if type(value) is float:
            return (
                (value, True)
                if math.isfinite(value) and value >= 0
                else (None, False)
            )
        return None, False

    @staticmethod
    def _format_salary_number(value: int | float) -> str:
        if value == 0:
            return "0"
        return str(value) if type(value) is int else format(value, "g")

    @staticmethod
    def _validated_publish_date(value: object) -> tuple[str | None, str | None]:
        if value is None:
            return None, None
        if type(value) in (int, float):
            if not math.isfinite(value) or not 946684800000 <= value <= 4102444800000:
                return None, "publish_date_format_not_approved"
            try:
                parsed = datetime.fromtimestamp(value / 1000, timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None, "publish_date_format_not_approved"
            return parsed.date().isoformat(), None
        if not isinstance(value, str):
            return None, "publish_date_format_not_approved"
        candidate = value.strip()
        if not candidate:
            return None, None
        for date_format in (
            "%Y-%m-%d",
            "%Y/%m/%d",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                parsed = datetime.strptime(candidate, date_format)
            except ValueError:
                continue
            if parsed.strftime(date_format) == candidate:
                return candidate, None
        return None, "publish_date_format_not_approved"

    @staticmethod
    def _page_title(soup: BeautifulSoup) -> str | None:
        if soup.title is None:
            return None
        title = " ".join(soup.title.get_text(" ", strip=True).split())
        return title or None

    @staticmethod
    def _single_detail_text(soup: BeautifulSoup, selector: str) -> str | None:
        nodes = soup.select(selector)
        if len(nodes) > 1:
            raise AdapterStructureError(
                f"NCSS detail response contains duplicate {selector} fields"
            )
        if not nodes:
            return None
        value = " ".join(nodes[0].get_text(" ", strip=True).split())
        return value or None

    @classmethod
    def _reject_page_title(
        cls, soup: BeautifulSoup, page_title: str | None
    ) -> None:
        prominent = (page_title or "").lower()
        error_markers = (
            "页面不存在",
            "服务异常",
            "系统错误",
            "访问错误",
            "not found",
            "forbidden",
            "404",
            "403",
        )
        if any(marker in prominent for marker in error_markers):
            raise AdapterStructureError("NCSS returned an error page")

        prominent_gate_markers = (
            "用户登录",
            "请先登录",
            "访问验证",
            "sign in to continue",
            "log in to continue",
            "verify you are human",
        )
        if any(marker in prominent for marker in prominent_gate_markers):
            raise AdapterStructureError("NCSS returned a login or verification page")
        if cls._has_gate_structure(soup) and any(
            marker in prominent
            for marker in (
                "sign in",
                "log in",
                "captcha",
                "登录验证",
                "安全验证",
            )
        ):
            raise AdapterStructureError("NCSS returned a login or verification page")

    @classmethod
    def _reject_job_description_gate(
        cls, soup: BeautifulSoup, description: str, page_title: str | None
    ) -> None:
        jd_text = " ".join(description.split()).lower()
        explicit_gate_markers = (
            "请先登录",
            "登录后查看",
            "访问验证",
            "安全验证",
            "请输入验证码",
            "sign in to continue",
            "log in to continue",
            "verify you are human",
        )
        error_markers = (
            "页面不存在",
            "服务异常",
            "系统错误",
            "访问错误",
            "not found",
            "forbidden",
        )
        job_markers = (
            "岗位职责",
            "岗位要求",
            "任职要求",
            "工作内容",
            "负责",
            "开发",
            "维护",
            "熟悉",
        )
        generic_gate_terms = ("sign in", "log in", "captcha", "登录", "验证码")
        short_error_main = (
            len(jd_text) <= 200
            and not any(marker in jd_text for marker in job_markers)
            and any(
                marker in jd_text
                for marker in (*error_markers, *generic_gate_terms)
            )
        )
        short_structural_gate = cls._has_gate_structure(soup) and short_error_main
        ambiguous_gate_title = any(
            marker in (page_title or "").lower()
            for marker in ("登录验证", "安全验证")
        )
        if any(marker in jd_text for marker in explicit_gate_markers) or (
            short_structural_gate
        ) or (
            ambiguous_gate_title and short_error_main
        ):
            raise AdapterStructureError("NCSS returned a login or verification page")

    @staticmethod
    def _has_gate_structure(soup: BeautifulSoup) -> bool:
        return (
            soup.select_one("input[type='password']") is not None
            or any(
                "login" in str(form.get("action", "")).lower()
                or "signin" in str(form.get("action", "")).lower()
                for form in soup.select("form")
            )
            or soup.select_one(
                "[id*='captcha' i], [class*='captcha' i], [name*='captcha' i]"
            )
            is not None
        )

    @staticmethod
    def _experience(soup: BeautifulSoup) -> str | None:
        for selector in (
            "[data-field='experience']",
            ".job-experience",
            ".experience",
        ):
            node = soup.select_one(selector)
            if node is not None:
                value = " ".join(node.get_text(" ", strip=True).split())
                if value:
                    return value
        return None

    @classmethod
    def _adapter_extra(cls, raw: dict[str, Any]) -> dict[str, object]:
        promoted = {
            "jobId",
            "jobName",
            "recName",
            "areaCodeName",
            "industrySectorsName",
            "industryName",
            "lowMonthPay",
            "highMonthPay",
            "degreeName",
            "publishDate",
        }
        extra: dict[str, object] = {
            key: value for key, value in raw.items() if key not in promoted
        }
        validation_issues: list[dict[str, object]] = []

        _, salary_issue = cls._validated_salary(
            raw.get("lowMonthPay"), raw.get("highMonthPay")
        )
        if salary_issue:
            salary_raw = {
                "lowMonthPay": cls._audit_value(raw.get("lowMonthPay")),
                "highMonthPay": cls._audit_value(raw.get("highMonthPay")),
            }
            extra.update(salary_raw)
            validation_issues.append(
                {"field": "salary", "code": salary_issue, "raw": salary_raw}
            )

        _, publish_date_issue = cls._validated_publish_date(raw.get("publishDate"))
        if publish_date_issue:
            publish_date_raw = cls._audit_value(raw.get("publishDate"))
            extra["publishDate"] = publish_date_raw
            validation_issues.append(
                {
                    "field": "publishDate",
                    "code": publish_date_issue,
                    "raw": publish_date_raw,
                }
            )

        if validation_issues:
            extra["validation_issues"] = validation_issues
        return extra

    @staticmethod
    def _audit_value(value: object) -> object:
        if type(value) is float and not math.isfinite(value):
            if math.isnan(value):
                return "NaN"
            return "Infinity" if value > 0 else "-Infinity"
        return value
