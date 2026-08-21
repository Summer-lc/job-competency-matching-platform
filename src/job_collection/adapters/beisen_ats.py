from __future__ import annotations

import json
import re
from datetime import datetime
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
_REQUEST_NUMBER = re.compile(r"\((J\d+)\)", re.IGNORECASE)


class BeisenATSAdapter(SourceAdapter):
    site_page_size = 50
    embedded_detail = True

    def __init__(
        self, *, source: SourceDefinition, registry: SourceRegistry
    ) -> None:
        if source.parser_name != "beisen_company_ats":
            raise ValueError("Beisen adapter requires beisen_company_ats source")
        if not source.organization_name:
            raise ValueError("Beisen source is missing reviewed company metadata")
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
        if offset % limit:
            raise ValueError("offset must align to the requested page size")
        if offset >= self.source.max_records or offset + limit > self.source.max_records:
            raise ValueError("request exceeds source record limit")
        url = self.registry.validate_url(
            self.source_id, "/api/Jobad/GetJobAdPageList"
        )
        return RequestSpec(
            method="POST",
            url=url,
            headers={
                "Origin": self.source.base_url,
                "Referer": f"{self.source.base_url}/social/jobs",
            },
            json_body={
                "PageIndex": offset // limit,
                "PageSize": limit,
                "LocId": [],
                "Category": ["1"],
                "KeyWords": keyword,
                "SpecialType": 0,
                "PortalId": "",
                "DisplayFields": [
                    "Category",
                    "Kind",
                    "LocId",
                    "PostDate",
                    "Salary",
                ],
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
            raise AdapterStructureError("Beisen list response is not JSON")
        try:
            document = json.loads(
                content.decode("utf-8"), parse_constant=self._reject_json_constant
            )
        except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
            raise AdapterStructureError("Beisen list response contains invalid JSON") from exc
        if not isinstance(document, dict):
            raise AdapterStructureError("Beisen list response root must be an object")
        records = document.get("Data")
        if not isinstance(records, list):
            raise AdapterStructureError("Beisen Data must be an array")
        total = document.get("Count")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise AdapterStructureError("Beisen Count must be a non-negative integer")
        offset = expected_offset if expected_offset is not None else 0
        limit = expected_limit if expected_limit is not None else max(1, len(records))
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("expected_offset must be a non-negative integer")
        maximum = min(self.site_page_size, self.source.max_records)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
            raise ValueError(f"expected_limit must be between 1 and {maximum}")
        if len(records) > limit:
            raise AdapterStructureError("Beisen response exceeds requested page size")
        if records and total < offset + len(records):
            raise AdapterStructureError("Beisen Count is smaller than returned records")
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
        self._safe_id(item.source_record_id)
        return self.registry.validate_url(self.source_id, "/social/jobs")

    def parse_detail(
        self, content: bytes, item: SourceJobRecord, url: str
    ) -> dict[str, object]:
        del content
        source_url = self.registry.validate_url(self.source_id, url)
        if source_url != self.build_detail_url(item):
            raise AdapterRecordError("Beisen detail URL does not match source portal")
        duty = self._text(item.raw.get("Duty"), "Duty")
        if not duty:
            raise AdapterRecordError("Beisen job description is empty")
        published_at = self._published_at(item.raw.get("PostDate"))
        recruitment_type = self._text(item.raw.get("Category"), "Category")
        title = item.job_title or ""
        match = _REQUEST_NUMBER.search(title)
        return {
            "source_id": self.source_id,
            "source_record_id": item.source_record_id,
            "job_title": item.job_title,
            "company_name": self.source.organization_name,
            "region": item.region,
            "salary": item.salary,
            "published_at": published_at,
            "published_at_evidence": (
                f"北森招聘列表字段 PostDate: {item.raw.get('PostDate')}"
                if published_at
                else None
            ),
            "published_at_confidence": 0.9 if published_at else 0.0,
            "job_description_raw": duty,
            "page_title": item.job_title,
            "response_status": 200,
            "source_url": source_url,
            "adapter_extra": {
                "ats_type": "beisen",
                "recruitment_type": recruitment_type,
                "request_number": match.group(1).upper() if match else "",
            },
        }

    def _parse_item(self, value: object) -> SourceJobRecord:
        if not isinstance(value, dict):
            raise AdapterRecordError("Beisen job record must be an object")
        source_record_id = self._safe_id(
            value.get("JobAdId") if value.get("JobAdId") is not None else value.get("Id")
        )
        title = self._text(value.get("JobAdName"), "JobAdName")
        if not title:
            raise AdapterRecordError("Beisen job title is empty")
        locations = value.get("LocNames")
        if locations is None:
            location_names: list[str] = []
        elif not isinstance(locations, list):
            raise AdapterRecordError("Beisen LocNames must be an array")
        else:
            location_names = []
            for location in locations:
                name = self._text(location, "LocNames item")
                if name:
                    location_names.append(name)
        salary = self._text(value.get("Salary"), "Salary") or None
        return SourceJobRecord(
            source_record_id=source_record_id,
            job_title=title,
            company_name=self.source.organization_name,
            region=", ".join(location_names) or None,
            salary=salary,
            published_at=self._published_at(value.get("PostDate")),
            raw=dict(value),
        )

    @staticmethod
    def _safe_id(value: object) -> str:
        job_id = str(value or "").strip()
        if not job_id or not _SAFE_ID.fullmatch(job_id):
            raise AdapterRecordError("Beisen job id is empty or unsafe")
        return job_id

    @staticmethod
    def _text(value: object, field: str) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise AdapterRecordError(f"Beisen {field} must be text")
        return value.strip()

    @staticmethod
    def _published_at(value: object) -> str | None:
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            raise AdapterRecordError("Beisen PostDate must be ISO text")
        candidate = value.strip()
        try:
            datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AdapterRecordError("Beisen PostDate must be ISO text") from exc
        return candidate

    @staticmethod
    def _reject_json_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")
