from __future__ import annotations

import csv
import hashlib
import io
import json
import posixpath
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from model_class.job_competency import JobPosting, JobPostingSkill, Skill
from schemes.job_competency import JobPostingInput
from src.bounded_json import JSONResourceLimitError, decode_json_array_incrementally
from src.observation import canonical_observation_payload
from src.skill_ontology import SKILL_CATALOG


class RecordImportLimitError(ValueError):
    """A structured record import exceeds a configured safety bound."""


MAX_RECORD_IMPORT_BYTES = 25 * 1024 * 1024
MAX_RECORD_IMPORT_RECORDS = 10_000
MAX_RECORD_IMPORT_LINE_BYTES = 1024 * 1024
MAX_RECORD_IMPORT_JSON_DEPTH = 50


JOB_FAMILY_NAMES = {
    "JAVA_DEVELOPER": "Java开发工程师",
    "PYTHON_BACKEND": "Python后端工程师",
    "GO_DEVELOPER": "Go开发工程师",
    "FRONTEND_DEVELOPER": "前端开发工程师",
    "DEVOPS_ENGINEER": "DevOps工程师",
    "SRE_ENGINEER": "SRE工程师",
    "CLOUD_NATIVE_ENGINEER": "云原生工程师",
    "AI_AGENT_ENGINEER": "AI智能体应用工程师",
    "LLM_APPLICATION_ENGINEER": "大模型应用工程师",
    "RAG_ENGINEER": "RAG工程师",
    "MLOPS_ENGINEER": "MLOps工程师",
    "MULTIMODAL_ENGINEER": "多模态算法工程师",
    "PROMPT_ENGINEER": "提示词工程师",
    "AI_SOLUTION_ENGINEER": "AI解决方案工程师",
    "BIG_DATA_DEVELOPER": "大数据开发工程师",
    "DATA_GOVERNANCE_ENGINEER": "数据治理工程师",
    "DATA_ENGINEER": "数据工程师",
    "IOT_ENGINEER": "物联网工程师",
    "EDGE_COMPUTING_ENGINEER": "边缘计算工程师",
    "CYBERSECURITY_ENGINEER": "网络安全工程师",
    "DIGITAL_TWIN_ENGINEER": "数字孪生工程师",
    "ROBOTICS_ENGINEER": "机器人与智能系统工程师",
}


SOURCE_SCORES = {
    "company_official": 0.95,
    "occupation_standard": 1.0,
    "technical_standard": 0.98,
    "policy_document": 0.95,
    "official_document": 0.92,
    "public_recruitment": 0.85,
    "university_recruitment": 0.8,
    "authorized_platform": 0.8,
    "open_dataset": 0.75,
    "public": 0.7,
}

PREFERRED_MARKERS = ("优先", "加分", "preferred", "plus", "更佳")
RESPONSIBILITY_MARKERS = ("负责", "职责", "参与", "建设", "开发", "维护", "推进", "设计")
SUSPICIOUS_INDUSTRY_PATTERN = re.compile(
    r"(?:\d+(?:\.\d+)?\s*[-—~至]\s*\d+(?:\.\d+)?\s*(?:元|[kK万]))|"
    r"(?:本科|硕士|大专|学历|岗位要求|职位要求|招聘)"
)


@dataclass(frozen=True)
class QualityFinding:
    code: str
    severity: str
    field_name: str | None
    message: str


def _parse_records_unbounded(raw: bytes, filename: str) -> List[dict]:
    text = raw.decode("utf-8-sig")
    lower_name = filename.lower()
    if lower_name.endswith(".jsonl"):
        records = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSONL第{line_number}行格式错误: {exc.msg}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"JSONL第{line_number}行必须是JSON对象")
            records.append(value)
        return records
    if lower_name.endswith(".json"):
        value = json.loads(text)
        if isinstance(value, dict):
            return [value]
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            return value
        raise ValueError("JSON文件必须是对象或对象数组")
    if lower_name.endswith(".csv"):
        return [dict(row) for row in csv.DictReader(io.StringIO(text))]
    raise ValueError("仅支持.jsonl、.json和.csv文件")


def _parse_records_incrementally(raw: bytes, filename: str) -> List[dict]:
    text = raw.decode("utf-8-sig")
    lower_name = filename.lower()
    records: list[dict] = []
    if lower_name.endswith(".jsonl"):
        for line_number, line in enumerate(io.StringIO(text), start=1):
            if not line.strip():
                continue
            if len(records) >= MAX_RECORD_IMPORT_RECORDS:
                raise RecordImportLimitError(
                    "record import record count exceeds limit "
                    f"{MAX_RECORD_IMPORT_RECORDS}: {len(records) + 1}"
                )
            try:
                value = json.loads(line)
            except RecursionError as exc:
                raise RecordImportLimitError(
                    "record import exceeds JSON decoder nesting resources"
                ) from exc
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"JSONL line {line_number} has invalid JSON: {exc.msg}"
                ) from exc
            if not isinstance(value, dict):
                raise ValueError(f"JSONL line {line_number} must be a JSON object")
            records.append(value)
        return records
    if lower_name.endswith(".json"):
        stripped = text.strip()
        if stripped.startswith("["):
            try:
                decoded = decode_json_array_incrementally(
                    stripped, max_records=MAX_RECORD_IMPORT_RECORDS
                )
            except JSONResourceLimitError as exc:
                raise RecordImportLimitError(str(exc)) from exc
            values = [item.value for item in decoded]
            if not all(isinstance(item, dict) for item in values):
                raise ValueError("JSON array items must be objects")
            return values
        try:
            value = json.loads(stripped)
        except RecursionError as exc:
            raise RecordImportLimitError(
                "record import exceeds JSON decoder nesting resources"
            ) from exc
        if isinstance(value, dict):
            return [value]
        raise ValueError("JSON file must be an object or an array of objects")
    if lower_name.endswith(".csv"):
        for row in csv.DictReader(io.StringIO(text)):
            if len(records) >= MAX_RECORD_IMPORT_RECORDS:
                raise RecordImportLimitError(
                    "record import record count exceeds limit "
                    f"{MAX_RECORD_IMPORT_RECORDS}: {len(records) + 1}"
                )
            records.append(dict(row))
        return records
    raise ValueError("only .jsonl, .json, and .csv files are supported")


def _validate_record_import_depth(value: object) -> None:
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if depth > MAX_RECORD_IMPORT_JSON_DEPTH:
            raise RecordImportLimitError(
                "record import exceeds JSON nesting limit "
                f"{MAX_RECORD_IMPORT_JSON_DEPTH}"
            )
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)


def _validate_record_json_depth_text(text: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_RECORD_IMPORT_JSON_DEPTH:
                raise RecordImportLimitError(
                    "record import exceeds JSON nesting limit "
                    f"{MAX_RECORD_IMPORT_JSON_DEPTH}"
                )
        elif character in "]}":
            depth = max(0, depth - 1)


def parse_records(raw: bytes, filename: str) -> List[dict]:
    if len(raw) > MAX_RECORD_IMPORT_BYTES:
        raise RecordImportLimitError(
            f"record import exceeds byte limit {MAX_RECORD_IMPORT_BYTES}: {len(raw)}"
        )
    for line_number, line in enumerate(io.BytesIO(raw), start=1):
        if len(line) > MAX_RECORD_IMPORT_LINE_BYTES:
            raise RecordImportLimitError(
                "record import line "
                f"{line_number} exceeds byte limit {MAX_RECORD_IMPORT_LINE_BYTES}"
            )
    if filename.lower().endswith((".json", ".jsonl")):
        try:
            _validate_record_json_depth_text(raw.decode("utf-8-sig"))
        except RecursionError as exc:
            raise RecordImportLimitError(
                "record import exceeds JSON nesting resources"
            ) from exc
    records = _parse_records_incrementally(raw, filename)
    if len(records) > MAX_RECORD_IMPORT_RECORDS:
        raise RecordImportLimitError(
            "record import record count exceeds limit "
            f"{MAX_RECORD_IMPORT_RECORDS}: {len(records)}"
        )
    for record in records:
        _validate_record_import_depth(record)
    return records


def _parse_datetime(value: object) -> datetime | None:
    if value in (None, "", "unknown"):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            parsed = datetime.strptime(raw[:10], "%Y-%m-%d")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def normalize_job_title(raw: str, family_code: str) -> str:
    if family_code in JOB_FAMILY_NAMES:
        return JOB_FAMILY_NAMES[family_code]
    title = re.sub(r"[（(].*?[）)]", "", raw)
    title = re.sub(r"^(?:[^-—]{1,10}[-—])", "", title)
    title = re.sub(r"^(?:初级|中级|高级|资深|专家|高级资深)", "", title)
    return title.strip()


def normalize_text(text: str) -> str:
    lowered = text.lower()
    lowered = re.sub(r"[\s\W_]+", "", lowered, flags=re.UNICODE)
    return lowered.replace("和", "").replace("及", "").replace("与", "")


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def simhash64(text: str) -> str:
    normalized = normalize_text(text)
    if not normalized:
        return "0" * 16
    features = [normalized[i : i + 2] for i in range(max(1, len(normalized) - 1))]
    weights = [0] * 64
    for feature in features:
        value = int.from_bytes(hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest(), "big")
        for bit in range(64):
            weights[bit] += 1 if value & (1 << bit) else -1
    fingerprint = sum(1 << bit for bit, weight in enumerate(weights) if weight >= 0)
    return f"{fingerprint:016x}"


def hamming_distance(first: str, second: str) -> int:
    return (int(first, 16) ^ int(second, 16)).bit_count()


def extract_skills(text: str) -> List[dict]:
    lowered = text.lower()
    hits: Dict[str, dict] = {}
    for name, (category, aliases) in SKILL_CATALOG.items():
        best_index = -1
        matched_alias = ""
        for alias in sorted(aliases, key=len, reverse=True):
            index = lowered.find(alias.lower())
            if index >= 0 and (best_index < 0 or index < best_index):
                best_index = index
                matched_alias = alias
        if best_index < 0:
            continue
        start = max(0, best_index - 18)
        end = min(len(text), best_index + len(matched_alias) + 24)
        evidence = text[start:end].strip()
        requirement = "preferred" if any(marker in evidence.lower() for marker in PREFERRED_MARKERS) else "required"
        hits[name] = {
            "name": name,
            "category": category,
            "requirement_type": requirement,
            "confidence": 0.92 if matched_alias.lower() == name.lower() else 0.86,
            "evidence_text": evidence,
        }
    return sorted(hits.values(), key=lambda item: item["name"].lower())


def extract_responsibilities(text: str) -> List[dict]:
    candidates: List[dict] = []
    seen = set()
    for match in re.finditer(r"[^。；;\n]{8,180}", text):
        value = re.sub(r"^\s*(?:\d+[、.)）]|[-•·])\s*", "", match.group()).strip()
        if not value or not any(marker in value for marker in RESPONSIBILITY_MARKERS):
            continue
        normalized = normalize_text(value)
        if normalized in seen:
            continue
        seen.add(normalized)
        start = text.find(value, match.start(), match.end())
        candidates.append(
            {
                "name": value,
                "category": "general",
                "confidence": 0.82,
                "evidence_text": value,
                "start_offset": start if start >= 0 else None,
                "end_offset": start + len(value) if start >= 0 else None,
            }
        )
        if len(candidates) >= 12:
            break
    return candidates


def assess_job_quality(prepared: dict) -> List[QualityFinding]:
    findings: List[QualityFinding] = []
    industry = str(prepared.get("industry") or "").strip()
    if industry and (len(industry) > 80 or SUSPICIOUS_INDUSTRY_PATTERN.search(industry)):
        findings.append(
            QualityFinding(
                code="suspicious_industry",
                severity="review",
                field_name="industry",
                message="行业字段疑似混入薪资、学历或岗位要求",
            )
        )
    published_at = prepared.get("published_at")
    collected_at = prepared.get("collected_at")
    if published_at and collected_at:
        day_gap = (collected_at - published_at).days
        if day_gap < -1 or day_gap > 3653:
            findings.append(
                QualityFinding(
                    code="suspicious_date",
                    severity="review",
                    field_name="published_at",
                    message="发布时间与采集时间相差异常",
                )
            )
    if not prepared.get("skills") and not prepared.get("responsibilities"):
        findings.append(
            QualityFinding(
                code="no_capability_evidence",
                severity="review",
                field_name="job_description_raw",
                message="未从岗位描述中识别到技能或职责",
            )
        )
    if float(prepared.get("quality_score") or 0.0) < 0.70:
        findings.append(
            QualityFinding(
                code="low_quality_score",
                severity="review",
                field_name=None,
                message="综合质量分低于0.70",
            )
        )
    return findings


def prepare_job_record(raw: dict) -> dict:
    try:
        validated = JobPostingInput(**raw)
    except Exception as exc:
        raise ValueError(str(exc)) from exc
    values = validated.model_dump()
    description = values["job_description_raw"]
    values.update(
        job_title_normalized=normalize_job_title(values["job_title_raw"], values["job_family_id"]),
        published_at=_parse_datetime(values.get("published_at")),
        collected_at=_parse_datetime(values.get("collected_at")) or datetime.now(),
        first_seen_at=_parse_datetime(values.get("first_seen_at")),
        last_seen_at=_parse_datetime(values.get("last_seen_at")),
        content_hash=content_hash(description),
        simhash=simhash64(description),
        source_score=SOURCE_SCORES.get(values.get("source_type") or "public", 0.65),
        skills=extract_skills(description),
        responsibilities=extract_responsibilities(description),
    )
    completeness = sum(
        bool(values.get(field))
        for field in ("company_name", "industry", "region", "published_at", "experience_requirement", "education_requirement")
    ) / 6
    description_score = min(len(description) / 500, 1.0)
    values["quality_score"] = round(
        0.45 * values["source_score"] + 0.30 * completeness + 0.25 * description_score,
        4,
    )
    values["raw_payload"] = canonical_observation_payload(raw)
    return values


async def _get_or_create_skill(db: AsyncSession, item: dict) -> Skill:
    skill = await db.scalar(select(Skill).where(Skill.name == item["name"]))
    if skill is None:
        skill = Skill(name=item["name"], category=item["category"], aliases_json="[]")
        db.add(skill)
        await db.flush()
    return skill


async def persist_prepared_job_record(
    db: AsyncSession, prepared: dict, *, existing: JobPosting | None = None
) -> tuple[JobPosting, bool]:
    values = dict(prepared)
    skills = list(values.pop("skills", []))
    values.pop("responsibilities", None)
    exclude_id = existing.id if existing is not None else None
    duplicate_query = select(JobPosting).where(JobPosting.content_hash == values["content_hash"])
    if exclude_id is not None:
        duplicate_query = duplicate_query.where(JobPosting.id != exclude_id)
    duplicate = await db.scalar(duplicate_query.order_by(JobPosting.id).limit(1))
    if duplicate is None:
        candidate_query = select(JobPosting).where(
            JobPosting.job_family_id == values["job_family_id"],
            JobPosting.duplicate_of_id.is_(None),
        )
        if exclude_id is not None:
            candidate_query = candidate_query.where(JobPosting.id != exclude_id)
        candidates = list(
            (await db.execute(candidate_query.order_by(JobPosting.id.desc()).limit(500)))
            .scalars()
            .all()
        )
        duplicate = next(
            (
                candidate
                for candidate in candidates
                if hamming_distance(candidate.simhash, values["simhash"]) <= 8
            ),
            None,
        )

    posting_fields = {
        key: value for key, value in values.items() if key in JobPosting.__table__.columns
    }
    if existing is None:
        posting = JobPosting(
            **posting_fields,
            duplicate_of_id=duplicate.id if duplicate else None,
            status="duplicate" if duplicate else "valid",
        )
        db.add(posting)
        await db.flush()
    else:
        posting = existing
        preserved_first_seen = posting.first_seen_at
        previous_last_seen = posting.last_seen_at
        for key, value in posting_fields.items():
            setattr(posting, key, value)
        if preserved_first_seen is not None and (
            posting.first_seen_at is None
            or posting.first_seen_at > preserved_first_seen
        ):
            posting.first_seen_at = preserved_first_seen
        if previous_last_seen is not None and (
            posting.last_seen_at is None or posting.last_seen_at < previous_last_seen
        ):
            posting.last_seen_at = previous_last_seen
        posting.observation_version += 1
        posting.duplicate_of_id = duplicate.id if duplicate else None
        posting.status = "duplicate" if duplicate else "valid"
        await db.execute(
            delete(JobPostingSkill).where(JobPostingSkill.job_posting_id == posting.id)
        )

    for item in skills:
        skill = await _get_or_create_skill(db, item)
        db.add(
            JobPostingSkill(
                job_posting_id=posting.id,
                skill_id=skill.id,
                requirement_type=item["requirement_type"],
                confidence=item["confidence"],
                evidence_text=item["evidence_text"],
            )
        )
    await db.flush()
    return posting, duplicate is not None


async def import_job_records(db: AsyncSession, records: Iterable[dict]) -> dict:
    summary = {"received": 0, "imported": 0, "duplicates": 0, "skipped": 0, "errors": []}
    for index, raw in enumerate(records, start=1):
        summary["received"] += 1
        try:
            prepared = prepare_job_record(raw)
            parsed_url = urlsplit(str(prepared.get("source_url") or "").strip())
            scheme = parsed_url.scheme.casefold()
            host = (parsed_url.hostname or "").rstrip(".").casefold()
            try:
                host = host.encode("idna").decode("ascii")
            except UnicodeError:
                host = ""
            port = parsed_url.port
            default_port = (scheme == "http" and port == 80) or (
                scheme == "https" and port == 443
            )
            netloc = host if port is None or default_port else f"{host}:{port}"
            path = posixpath.normpath(parsed_url.path or "/")
            if not path.startswith("/"):
                path = f"/{path}"
            prepared.update(
                source_id=None,
                source_type="unknown",
                source_domain=host or None,
                source_url=urlunsplit((scheme, netloc, path, parsed_url.query, "")),
                parser_name=None,
                parser_version=None,
                collection_method=None,
                provenance_status="unverified",
                published_at_trusted=False,
                source_score=0.0,
            )
            existed = await db.scalar(
                select(JobPosting).where(JobPosting.record_id == prepared["record_id"])
            )
            if existed is not None:
                summary["skipped"] += 1
                continue
            duplicate = await db.scalar(
                select(JobPosting)
                .where(JobPosting.content_hash == prepared["content_hash"])
                .order_by(JobPosting.id)
                .limit(1)
            )
            if duplicate is None:
                candidates = list(
                    (
                        await db.execute(
                            select(JobPosting)
                            .where(
                                JobPosting.job_family_id == prepared["job_family_id"],
                                JobPosting.duplicate_of_id.is_(None),
                            )
                            .order_by(JobPosting.id.desc())
                            .limit(500)
                        )
                    )
                    .scalars()
                    .all()
                )
                duplicate = next(
                    (
                        candidate
                        for candidate in candidates
                        if hamming_distance(candidate.simhash, prepared["simhash"]) <= 8
                    ),
                    None,
                )
            skills = prepared.pop("skills")
            posting_fields = {key: value for key, value in prepared.items() if key in JobPosting.__table__.columns}
            posting = JobPosting(
                **posting_fields,
                duplicate_of_id=duplicate.id if duplicate else None,
                status="duplicate" if duplicate else "review",
                gate_status="review",
            )
            db.add(posting)
            await db.flush()
            if duplicate:
                summary["duplicates"] += 1
            for item in skills:
                skill = await _get_or_create_skill(db, item)
                db.add(
                    JobPostingSkill(
                        job_posting_id=posting.id,
                        skill_id=skill.id,
                        requirement_type=item["requirement_type"],
                        confidence=item["confidence"],
                        evidence_text=item["evidence_text"],
                    )
                )
            summary["imported"] += 1
        except Exception as exc:
            summary["errors"].append({"row": index, "message": str(exc)})
    await db.commit()
    return summary
