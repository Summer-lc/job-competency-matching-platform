from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

import httpx

from src.job_collection.adapters import BeisenATSAdapter, FeishuATSAdapter
from src.job_collection.adapters.base import AdapterRecordError, AdapterStructureError
from src.job_collection.models import SourceDefinition
from src.job_collection.source_registry import SourceRegistry


_FEISHU_SUFFIX = ".jobs.feishu.cn"
_BEISEN_SUFFIX = ".zhiye.com"
_SAFE_PORTAL_PATH = re.compile(r"^[A-Za-z0-9_/-]{1,40}$")
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_BLOCKED_RESPONSE_KEYS = {
    "applicant",
    "applicants",
    "candidateemail",
    "candidatephone",
    "email",
    "mobile",
    "phone",
    "resume",
    "resumes",
}
_ACCESS_MARKERS = (
    "captcha",
    "verify you are human",
    "login required",
    "请登录",
    "验证码",
    "人机验证",
)


class _WebsiteInfoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._inside = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "script":
            return
        values = {name.casefold(): value for name, value in attrs}
        self._inside = values.get("id") == "js-websiteInfo"

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script":
            self._inside = False

    def handle_data(self, data: str) -> None:
        if self._inside:
            self.parts.append(data)


@dataclass(frozen=True)
class _Response:
    status_code: int
    content_type: str
    content: bytes


def _host_matches(host: str, suffix: str) -> bool:
    return host.endswith(suffix) and host != suffix.removeprefix(".")


def _validate_candidate(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("candidate must be an object")
    required = (
        "source_id",
        "organization_name",
        "ats_type",
        "portal_host",
        "reviewed_homepage_url",
        "status",
    )
    candidate: dict[str, str] = {}
    for field in required:
        raw = value.get(field)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"candidate {field} must be a non-empty string")
        candidate[field] = raw.strip()
    if candidate["status"] != "candidate":
        raise ValueError("candidate status must be candidate and grants no approval")
    if not re.fullmatch(r"company_(?:feishu|beisen)_[a-z0-9_]+", candidate["source_id"]):
        raise ValueError("candidate source_id is invalid")
    ats_type = candidate["ats_type"]
    host = candidate["portal_host"].casefold().rstrip(".")
    expected_suffix = _FEISHU_SUFFIX if ats_type == "feishu" else _BEISEN_SUFFIX
    if ats_type not in {"feishu", "beisen"} or not _host_matches(host, expected_suffix):
        raise ValueError("candidate host is outside an approved ATS domain")
    parsed = urlsplit(candidate["reviewed_homepage_url"])
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold().rstrip(".") != host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("reviewed_homepage_url must be a clean HTTPS URL on portal_host")
    candidate["portal_host"] = host
    return candidate


def load_candidates(path: str | Path) -> list[dict[str, str]]:
    candidate_path = Path(path)
    if candidate_path.stat().st_size > 1024 * 1024:
        raise ValueError("candidate inventory exceeds 1 MiB")
    document = json.loads(candidate_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError("candidate inventory schema_version must be 1")
    values = document.get("candidates")
    if not isinstance(values, list) or not values:
        raise ValueError("candidate inventory must contain candidates")
    candidates = [_validate_candidate(value) for value in values]
    source_ids = [value["source_id"] for value in candidates]
    hosts = [value["portal_host"] for value in candidates]
    if len(set(source_ids)) != len(source_ids) or len(set(hosts)) != len(hosts):
        raise ValueError("candidate source IDs and hosts must be unique")
    return candidates


async def _request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, object] | None = None,
) -> _Response:
    request_headers = {
        "User-Agent": "JobCompetencyResearchCollector/1.0",
        **(headers or {}),
    }
    payload = None
    if json_body is not None:
        payload = json.dumps(
            json_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    client.cookies.clear()
    request = client.build_request(method, url, headers=request_headers, content=payload)
    for name in (
        "authorization",
        "proxy-authorization",
        "cookie",
        "x-api-key",
        "x-auth-token",
    ):
        request.headers.pop(name, None)
    response = await client.send(request, stream=True, follow_redirects=False)
    try:
        if response.is_redirect:
            raise ValueError(f"redirect refused: HTTP {response.status_code}")
        declared = response.headers.get("content-length")
        if declared is not None and int(declared) > _MAX_RESPONSE_BYTES:
            raise ValueError("response exceeds 2 MiB")
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > _MAX_RESPONSE_BYTES:
                raise ValueError("response exceeds 2 MiB")
            chunks.append(chunk)
        return _Response(
            status_code=response.status_code,
            content_type=response.headers.get("content-type", ""),
            content=b"".join(chunks),
        )
    finally:
        await response.aclose()
        client.cookies.clear()


def _discover_feishu_path(content: bytes) -> str:
    try:
        html = content.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Feishu homepage is not UTF-8") from exc
    parser = _WebsiteInfoParser()
    parser.feed(html)
    raw = "".join(parser.parts).strip()
    if not raw:
        raise ValueError("Feishu homepage lacks js-websiteInfo")
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise ValueError("Feishu website info is malformed") from exc

    def find_path(value: object) -> str | None:
        if isinstance(value, dict):
            path = value.get("path")
            if isinstance(path, str) and path.strip():
                return path.strip()
            for child in value.values():
                found = find_path(child)
                if found:
                    return found
        return None

    path = find_path(document)
    if path is None or not _SAFE_PORTAL_PATH.fullmatch(path):
        raise ValueError("Feishu portal path is missing or unsafe")
    return path.strip("/")


def _source_for(candidate: dict[str, str], portal_path: str | None) -> SourceDefinition:
    is_feishu = candidate["ats_type"] == "feishu"
    return SourceDefinition.model_validate(
        {
            "source_id": candidate["source_id"],
            "source_name": f"{candidate['organization_name']}官方招聘",
            "source_type": "company_official",
            "market_scope": "pending_review",
            "base_url": f"https://{candidate['portal_host']}",
            "allowed_paths": (
                ["/api/v1/search/job/posts", f"/{portal_path}/position/"]
                if is_feishu
                else ["/api/Jobad/GetJobAdPageList", "/social/jobs"]
            ),
            "collection_mode": "public_json",
            "compliance_status": "pending_review",
            "compliance_note": "只读候选预检，不授予采集权限",
            "rate_limit_seconds": 3.0,
            "max_pages": 1,
            "max_records": 1,
            "parser_name": "feishu_company_ats" if is_feishu else "beisen_company_ats",
            "parser_version": "v1",
            "organization_name": candidate["organization_name"],
            "portal_path": portal_path if is_feishu else None,
            "enabled": False,
        }
    )


def _contains_personal_applicant_data(value: object) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z]", "", str(key).casefold())
            if normalized in _BLOCKED_RESPONSE_KEYS:
                return True
            if _contains_personal_applicant_data(child):
                return True
    elif isinstance(value, list):
        return any(_contains_personal_applicant_data(child) for child in value)
    return False


async def _preflight_one(
    candidate: dict[str, str], client: httpx.AsyncClient, checked_at: str
) -> dict[str, object]:
    result: dict[str, object] = {
        **candidate,
        "checked_at": checked_at,
        "accepted": False,
        "portal_path": None,
        "http_status": None,
        "sample_count": 0,
        "response_sha256": None,
        "field_presence": {},
        "stop_reason": None,
    }
    try:
        portal_path = None
        if candidate["ats_type"] == "feishu":
            homepage = await _request(
                client, "GET", candidate["reviewed_homepage_url"]
            )
            if homepage.status_code != 200:
                raise ValueError(f"homepage HTTP {homepage.status_code}")
            portal_path = _discover_feishu_path(homepage.content)
            result["portal_path"] = portal_path
        source = _source_for(candidate, portal_path)
        registry = SourceRegistry([source])
        adapter = (
            FeishuATSAdapter(source=source, registry=registry)
            if candidate["ats_type"] == "feishu"
            else BeisenATSAdapter(source=source, registry=registry)
        )
        request = adapter.build_list_request("", 0, 1)
        response = await _request(
            client,
            request.method,
            request.url,
            headers=request.headers,
            json_body=request.json_body,
        )
        result["http_status"] = response.status_code
        result["response_sha256"] = hashlib.sha256(response.content).hexdigest()
        if response.status_code != 200:
            raise ValueError(f"ATS HTTP {response.status_code}")
        lowered = response.content[:100_000].decode("utf-8", errors="ignore").casefold()
        if any(marker in lowered for marker in _ACCESS_MARKERS):
            raise ValueError("login or captcha access gate detected")
        try:
            raw_document = json.loads(response.content)
        except ValueError as exc:
            raise ValueError("malformed ATS response") from exc
        if _contains_personal_applicant_data(raw_document):
            raise ValueError("response contains applicant personal data fields")
        try:
            page = adapter.parse_list(
                response.content,
                response.content_type,
                expected_offset=0,
                expected_limit=1,
            )
        except (AdapterRecordError, AdapterStructureError, ValueError, TypeError) as exc:
            raise ValueError(f"malformed ATS response: {exc}") from exc
        if not page.items:
            raise ValueError("empty ATS response")
        item = page.items[0]
        detail_url = adapter.build_detail_url(item)
        detail = adapter.parse_detail(response.content, item, detail_url)
        region_key = "city_list" if candidate["ats_type"] == "feishu" else "LocNames"
        presence = {
            "title": bool(item.job_title),
            "record_id": bool(item.source_record_id),
            "description": bool(detail.get("job_description_raw")),
            "company": detail.get("company_name") == candidate["organization_name"],
            "region_field": region_key in item.raw,
            "traceable_url": str(detail.get("source_url", "")).startswith("https://"),
        }
        if not all(presence.values()):
            raise ValueError("required field completeness check failed")
        result["sample_count"] = len(page.items)
        result["field_presence"] = presence
        result["accepted"] = True
    except (httpx.HTTPError, ValueError, AdapterRecordError, AdapterStructureError) as exc:
        result["stop_reason"] = str(exc)
    return result


async def preflight_candidates(
    candidates: Iterable[dict[str, str]],
    *,
    client: httpx.AsyncClient | None = None,
    checked_at: str | None = None,
    delay_seconds: float = 0.0,
) -> dict[str, object]:
    if delay_seconds < 0:
        raise ValueError("delay_seconds cannot be negative")
    values = [_validate_candidate(value) for value in candidates]
    timestamp = checked_at or datetime.now(timezone.utc).isoformat()
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=15.0, follow_redirects=False)
    results: list[dict[str, object]] = []
    try:
        for index, candidate in enumerate(values):
            if index and delay_seconds:
                await asyncio.sleep(delay_seconds)
            results.append(await _preflight_one(candidate, http_client, timestamp))
    finally:
        http_client.cookies.clear()
        if owns_client:
            await http_client.aclose()
    accepted_count = sum(result["accepted"] is True for result in results)
    return {
        "schema_version": 1,
        "checked_at": timestamp,
        "candidate_count": len(results),
        "accepted_count": accepted_count,
        "rejected_count": len(results) - accepted_count,
        "results": results,
    }


def _write_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读预检中国企业公开招聘门户")
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-sources", type=int, default=35)
    parser.add_argument("--delay-seconds", type=float, default=3.0)
    args = parser.parse_args(argv)
    if args.max_sources < 1 or args.max_sources > 100:
        parser.error("--max-sources must be between 1 and 100")
    candidates = load_candidates(args.candidates)[: args.max_sources]
    report = asyncio.run(
        preflight_candidates(candidates, delay_seconds=args.delay_seconds)
    )
    _write_report(Path(args.output), report)
    print(
        json.dumps(
            {
                "output": str(Path(args.output)),
                "candidate_count": report["candidate_count"],
                "accepted_count": report["accepted_count"],
                "rejected_count": report["rejected_count"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
