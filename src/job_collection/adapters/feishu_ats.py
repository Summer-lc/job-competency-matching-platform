from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Mapping

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


_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class FeishuATSAdapter(SourceAdapter):
    site_page_size = 50
    embedded_detail = True

    def __init__(
        self, *, source: SourceDefinition, registry: SourceRegistry
    ) -> None:
        if source.parser_name != "feishu_company_ats":
            raise ValueError("Feishu adapter requires feishu_company_ats source")
        if not source.organization_name or not source.portal_path:
            raise ValueError("Feishu source is missing reviewed company metadata")
        self.source = source
        self.source_id = source.source_id
        self.registry = registry

    def build_list_request(
        self, query: str | Mapping[str, object], offset: int, limit: int
    ) -> RequestSpec:
        if isinstance(query, Mapping):
            keyword = str(query.get("query") or query.get("keyword") or "").strip()
        else:
            keyword = str(query).strip()
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        maximum = min(self.site_page_size, self.source.max_records)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
            raise ValueError(f"limit must be between 1 and {maximum}")
        if offset >= self.source.max_records or offset + limit > self.source.max_records:
            raise ValueError("request exceeds source record limit")
        url = self.registry.validate_url(
            self.source_id, "/api/v1/search/job/posts"
        )
        return RequestSpec(
            method="POST",
            url=url,
            headers={
                "Origin": self.source.base_url,
                "Referer": f"{self.source.base_url}/",
                "Portal-Channel": "office",
                "Portal-Platform": "pc",
                "website-path": self.source.portal_path,
            },
            json_body={
                "keyword": keyword,
                "limit": limit,
                "offset": offset,
                "portal_type": 2,
                "job_category_id_list": [],
                "location_code_list": [],
                "subject_id_list": [],
                "recruitment_id_list": [],
                "job_function_id_list": [],
            },
        )

    def parse_list(
        self,
        content: bytes,
        content_type: str | None,
        expected_offset: int | None = None,
        expected_limit: int | None = None,
    ) -> ListPage:
        if content_type and "json" not in content_type.casefold():
            raise AdapterStructureError("Feishu list response is not JSON")
        try:
            document = json.loads(
                content.decode("utf-8"), parse_constant=self._reject_json_constant
            )
        except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
            raise AdapterStructureError("Feishu list response contains invalid JSON") from exc
        if not isinstance(document, dict):
            raise AdapterStructureError("Feishu list response root must be an object")
        if document.get("code") != 0:
            raise AdapterStructureError("Feishu list response code is not zero")
        data = document.get("data")
        if not isinstance(data, dict):
            raise AdapterStructureError("Feishu list response is missing data object")
        records = data.get("job_post_list")
        if not isinstance(records, list):
            raise AdapterStructureError("Feishu data.job_post_list must be an array")
        total = data.get("count")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise AdapterStructureError("Feishu data.count must be a non-negative integer")
        offset = expected_offset if expected_offset is not None else 0
        limit = expected_limit if expected_limit is not None else max(1, len(records))
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("expected_offset must be a non-negative integer")
        maximum = min(self.site_page_size, self.source.max_records)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
            raise ValueError(f"expected_limit must be between 1 and {maximum}")
        if len(records) > limit:
            raise AdapterStructureError("Feishu response exceeds requested page size")
        if records and total < offset + len(records):
            raise AdapterStructureError("Feishu count is smaller than returned records")
        items = tuple(self._parse_item(record) for record in records)
        bounded_total = min(total, self.source.max_records)
        return ListPage(
            items=items,
            total=bounded_total,
            offset=offset,
            limit=limit,
            has_more=(
                len(items) == limit and offset + len(items) < bounded_total
            ),
        )

    def build_detail_url(self, item: SourceJobRecord) -> str:
        job_id = self._safe_id(item.source_record_id)
        path = f"/{self.source.portal_path}/position/{job_id}/detail"
        return self.registry.validate_url(self.source_id, path)

    def parse_detail(
        self, content: bytes, item: SourceJobRecord, url: str
    ) -> dict[str, object]:
        del content
        source_url = self.registry.validate_url(self.source_id, url)
        if source_url != self.build_detail_url(item):
            raise AdapterRecordError("Feishu detail URL does not match list record")
        raw = item.raw
        description = self._text(raw.get("description"), "description")
        requirement = self._text(raw.get("requirement"), "requirement")
        job_description = "\n\n".join(
            value for value in (description, requirement) if value
        )
        if not job_description:
            raise AdapterRecordError("Feishu job description is empty")
        published_at = self._published_at(raw.get("publish_time"))
        department = self._named_value(raw.get("job_function"), "job_function")
        recruitment_type = self._named_value(raw.get("recruit_type"), "recruit_type")
        return {
            "source_id": self.source_id,
            "source_record_id": item.source_record_id,
            "job_title": item.job_title,
            "company_name": self.source.organization_name,
            "region": item.region,
            "published_at": published_at,
            "published_at_evidence": (
                f"飞书招聘列表字段 publish_time: {raw.get('publish_time')}"
                if published_at
                else None
            ),
            "published_at_confidence": 0.9 if published_at else 0.0,
            "job_description_raw": job_description,
            "page_title": item.job_title,
            "response_status": 200,
            "source_url": source_url,
            "adapter_extra": {
                "ats_type": "feishu",
                "department": department,
                "recruitment_type": recruitment_type,
            },
        }

    def _parse_item(self, value: object) -> SourceJobRecord:
        if not isinstance(value, dict):
            raise AdapterRecordError("Feishu job record must be an object")
        source_record_id = self._safe_id(value.get("id"))
        title = self._text(value.get("title"), "title")
        if not title:
            raise AdapterRecordError("Feishu job title is empty")
        cities = value.get("city_list")
        if cities is None:
            city_names: list[str] = []
        elif not isinstance(cities, list):
            raise AdapterRecordError("Feishu city_list must be an array")
        else:
            city_names = []
            for city in cities:
                name = self._named_value(city, "city_list")
                if name:
                    city_names.append(name)
        return SourceJobRecord(
            source_record_id=source_record_id,
            job_title=title,
            company_name=self.source.organization_name,
            region=", ".join(city_names) or None,
            published_at=self._published_at(value.get("publish_time")),
            raw=dict(value),
        )

    @staticmethod
    def _safe_id(value: object) -> str:
        job_id = str(value or "").strip()
        if not job_id or not _SAFE_ID.fullmatch(job_id):
            raise AdapterRecordError("Feishu job id is empty or unsafe")
        return job_id

    @staticmethod
    def _text(value: object, field: str) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise AdapterRecordError(f"Feishu {field} must be text")
        return value.strip()

    @classmethod
    def _named_value(cls, value: object, field: str) -> str:
        if value is None:
            return ""
        if not isinstance(value, dict):
            raise AdapterRecordError(f"Feishu {field} must be an object")
        return cls._text(value.get("name"), f"{field}.name")

    @staticmethod
    def _published_at(value: object) -> str | None:
        if value in (None, ""):
            return None
        if isinstance(value, bool):
            raise AdapterRecordError("Feishu publish_time must be epoch milliseconds")
        try:
            milliseconds = float(value)
        except (TypeError, ValueError) as exc:
            raise AdapterRecordError(
                "Feishu publish_time must be epoch milliseconds"
            ) from exc
        if not math.isfinite(milliseconds) or milliseconds < 0:
            raise AdapterRecordError("Feishu publish_time must be epoch milliseconds")
        try:
            return datetime.fromtimestamp(
                milliseconds / 1000, timezone.utc
            ).isoformat()
        except (OverflowError, OSError, ValueError) as exc:
            raise AdapterRecordError("Feishu publish_time is out of range") from exc

    @staticmethod
    def _reject_json_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")
