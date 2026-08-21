from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from config.DB_config import ASYNC_DATABASE_URL, AsyncSessionLocal
from model_class.job_competency import JobPosting
from src.job_collection.adapters import AuthorizedExportAdapter
from src.job_collection.authorization import load_authorized_source_grants
from src.job_collection.coverage import (
    build_coverage_report,
    load_collection_targets,
    recommend_batches,
)
from src.job_collection.service import (
    DEFAULT_COLLECTIONS_ROOT,
    DEFAULT_REGISTRY_PATH,
    CollectionService,
    commit_collection_run,
)
from src.job_collection.source_registry import SourceRegistry
from src.job_collection.http_client import SourceStopped
from src.job_collection.storage import StorageError
from src.schema_migration import DatabaseOperationalError


DEFAULT_BACKUP_DIR = Path(__file__).resolve().parents[1] / "data" / "backups"
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_RECORDS = 10_000
MAX_PAGES = 100
MAX_REQUESTS = 10_000
AUTHORIZED_EXPORT_SOURCE_IDS = frozenset(
    {
        "boss_zhipin_authorized",
        "job51_authorized",
        "liepin_authorized",
        "lagou_authorized",
        "newjobs_authorized",
        "jobonline_authorized",
    }
)
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect reviewed public job sources into validated staging artifacts.",
        epilog="Exit codes: 2 arguments/report, 3 storage/lock, 4 network, 5 database.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Stage files without DB writes.")
    mode.add_argument("--commit", action="store_true", help="Import a completed staging run.")
    mode.add_argument(
        "--authorization-preflight",
        action="store_true",
        help="Validate one authorized export without staging or DB writes.",
    )
    mode.add_argument(
        "--coverage-report",
        action="store_true",
        help="Report current collection gaps without collection writes.",
    )
    parser.add_argument("--source", action="append", help="Reviewed source id; repeatable.")
    parser.add_argument("--run-id", help="Safe id for a new dry-run.")
    parser.add_argument("--resume-run", help="Resume or commit an existing run id.")
    parser.add_argument("--max-records", type=int, default=20)
    parser.add_argument("--record-offset", type=int, default=0)
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--max-requests", type=int, default=100)
    parser.add_argument("--manifest", type=Path, help="Explicit local manual-only manifest.")
    parser.add_argument(
        "--input-file", type=Path, help="Explicit authorized local JSONL export."
    )
    parser.add_argument(
        "--authorization-note",
        help="Required confirmation describing the local file authorization boundary.",
    )
    parser.add_argument(
        "--authorization-manifest",
        type=Path,
        help="Local grant manifest for an explicitly authorized platform export.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write coverage JSON under data/expansion-reports.",
    )
    parser.add_argument("--confirm", action="store_true", help="Required with --commit.")
    parser.add_argument("--debug", action="store_true", help="Raise operational errors.")
    return parser


def _validate_run_id(value: str | None, label: str) -> None:
    if value is None:
        return
    device_name = value.split(".", 1)[0].upper()
    if (
        not _RUN_ID_PATTERN.fullmatch(value)
        or value.endswith((".", " "))
        or device_name in _WINDOWS_RESERVED_NAMES
    ):
        raise ValueError(f"{label} is invalid")


def validate_args(args: argparse.Namespace) -> None:
    _validate_run_id(args.run_id, "--run-id")
    _validate_run_id(args.resume_run, "--resume-run")
    if args.output and not args.coverage_report:
        raise ValueError("--output is limited to --coverage-report")
    if not 1 <= args.max_records <= MAX_RECORDS:
        raise ValueError(f"--max-records must be between 1 and {MAX_RECORDS}")
    if not 0 <= args.record_offset <= MAX_RECORDS:
        raise ValueError(f"--record-offset must be between 0 and {MAX_RECORDS}")
    if not 1 <= args.max_pages <= MAX_PAGES:
        raise ValueError(f"--max-pages must be between 1 and {MAX_PAGES}")
    if not 1 <= args.max_requests <= MAX_REQUESTS:
        raise ValueError(f"--max-requests must be between 1 and {MAX_REQUESTS}")
    if args.source and len(args.source) != len(set(args.source)):
        raise ValueError("--source values must be unique")
    if args.source and len(args.source) > 4:
        raise ValueError("at most four --source values are allowed")
    if args.record_offset and not (
        args.dry_run
        and not args.resume_run
        and args.source
        and len(args.source) == 1
        and args.source[0] in AUTHORIZED_EXPORT_SOURCE_IDS
        and args.input_file
        and args.authorization_manifest
    ):
        raise ValueError(
            "--record-offset is limited to a new authorized-export dry-run"
        )

    if args.coverage_report:
        if (
            args.source
            or args.run_id
            or args.resume_run
            or args.manifest
            or args.input_file
            or args.authorization_note
            or args.authorization_manifest
            or args.confirm
        ):
            raise ValueError("--coverage-report does not accept collection options")
        return

    if args.authorization_preflight:
        if (
            not args.source
            or len(args.source) != 1
            or args.source[0] not in AUTHORIZED_EXPORT_SOURCE_IDS
            or not args.input_file
            or not args.authorization_manifest
        ):
            raise ValueError(
                "--authorization-preflight requires one authorized source, --input-file, and --authorization-manifest"
            )
        if (
            args.run_id
            or args.resume_run
            or args.manifest
            or args.authorization_note
            or args.confirm
        ):
            raise ValueError("--authorization-preflight does not accept staging options")
        if args.input_file.suffix.casefold() not in {".jsonl", ".json", ".csv"}:
            raise ValueError("authorized export must use .jsonl, .json, or .csv")
        return

    if args.commit:
        if not args.confirm:
            raise ValueError("--commit requires --confirm")
        if not args.resume_run:
            raise ValueError("--commit requires --resume-run")
        if (
            args.source
            or args.run_id
            or args.manifest
            or args.input_file
            or args.authorization_note
        ):
            raise ValueError(
                "--commit accepts only --resume-run, --confirm, and optionally --authorization-manifest"
            )
    else:
        if args.confirm:
            raise ValueError("--confirm is only valid with --commit")
        if args.resume_run:
            if (
                args.source
                or args.run_id
                or args.manifest
                or args.input_file
                or args.authorization_note
            ):
                raise ValueError("resume dry-run cannot select a new source or manifest")
        elif not args.source:
            raise ValueError("a new dry-run requires at least one --source")
        if args.manifest and args.source != ["company_official_manifest"]:
            raise ValueError("--manifest is limited to company_official_manifest")
        legacy_file_options = bool(args.input_file or args.authorization_note)
        authorized_export = bool(
            args.source
            and len(args.source) == 1
            and args.source[0] in AUTHORIZED_EXPORT_SOURCE_IDS
        )
        if authorized_export:
            if not args.input_file or not args.authorization_manifest:
                raise ValueError(
                    "authorized platform export requires --input-file and --authorization-manifest"
                )
            if args.authorization_note or args.manifest:
                raise ValueError(
                    "authorized platform export cannot use --authorization-note or --manifest"
                )
        elif args.authorization_manifest:
            raise ValueError(
                "--authorization-manifest is limited to one authorized platform export source"
            )
        if legacy_file_options and not authorized_export and not (
            args.input_file and args.authorization_note
        ):
            raise ValueError(
                "--input-file and --authorization-note must be provided together"
            )
        if legacy_file_options and not authorized_export and (
            args.source != ["zhaopin_legacy_import"] or args.manifest
        ):
            raise ValueError(
                "--input-file is limited to zhaopin_legacy_import and cannot use --manifest"
            )
        if args.source == ["zhaopin_legacy_import"] and not legacy_file_options:
            raise ValueError(
                "zhaopin_legacy_import requires --input-file and --authorization-note"
            )
        if args.input_file:
            allowed_suffixes = {".jsonl", ".json", ".csv"} if authorized_export else {".jsonl"}
            if args.input_file.suffix.casefold() not in allowed_suffixes:
                raise ValueError(
                    "--input-file must use .jsonl, .json, or .csv"
                    if authorized_export
                    else "--input-file must use .jsonl"
                )


def _authorized_preflight_quality(
    records: Sequence[object], *, minimum_year: int, maximum_year: int
) -> dict[str, object]:
    statuses: Counter[str] = Counter()
    families: Counter[str] = Counter()
    trusted_window = 0
    for record in records:
        extra = dict(getattr(record, "adapter_extra", {}) or {})
        gate = dict(extra.get("quality_gate") or {})
        status = str(gate.get("status") or "quarantined")
        statuses[status] += 1
        if status == "valid":
            family = str(getattr(record, "job_family_id", "UNKNOWN"))
            families[family] += 1
            published_at = getattr(record, "published_at", None)
            trusted = bool(getattr(record, "published_at_trusted", False))
            if (
                trusted
                and published_at is not None
                and minimum_year <= published_at.year <= maximum_year
            ):
                trusted_window += 1
    return {
        "valid_candidate_count": statuses["valid"],
        "review_candidate_count": statuses["review"],
        "quarantined_candidate_count": statuses["quarantined"],
        "trusted_window_candidate_count": trusted_window,
        "candidate_family_counts": dict(sorted(families.items())),
    }


async def execute(args: argparse.Namespace) -> dict[str, object]:
    validate_args(args)
    if args.authorization_preflight:
        registry = SourceRegistry.load(DEFAULT_REGISTRY_PATH)
        source = registry.get(args.source[0])
        grants = load_authorized_source_grants(
            args.authorization_manifest,
            today=date.today(),
        )
        grant = grants.require(source.source_id, "file_export")
        if any(
            marker in grant.authorization_reference.casefold()
            for marker in ("fixture", "example", "sample", "test-only")
        ):
            raise ValueError("production authorization reference cannot be a fixture")
        adapter = AuthorizedExportAdapter(source=source, registry=registry)
        records = adapter.load_file(
            args.input_file,
            run_id="authorization-preflight",
            authorization_reference=grant.authorization_reference,
            authorization_scope=grant.scope,
            max_records=source.max_records,
            collected_at=datetime.now(timezone.utc),
        )
        payload = AuthorizedExportAdapter._read_file(args.input_file)
        targets = load_collection_targets()
        quality = _authorized_preflight_quality(
            records,
            minimum_year=targets.minimum_published_year,
            maximum_year=targets.maximum_published_year,
        )
        return {
            "source_id": source.source_id,
            "authorization_reference": grant.authorization_reference,
            "valid_until": grant.valid_until.isoformat(),
            "access_method": "file_export",
            "input_filename": args.input_file.name,
            "input_file_sha256": hashlib.sha256(payload).hexdigest(),
            "row_count": len(records) + len(adapter.errors),
            "accepted_count": len(records),
            "rejected_count": len(adapter.errors),
            **quality,
        }

    if args.coverage_report:
        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    select(
                        JobPosting.id,
                        JobPosting.record_id,
                        JobPosting.content_hash,
                        JobPosting.job_family_id,
                        JobPosting.source_id,
                        JobPosting.source_domain,
                        JobPosting.source_type,
                        JobPosting.region,
                        JobPosting.published_at,
                        JobPosting.published_at_trusted,
                        JobPosting.gate_status,
                        JobPosting.provenance_status,
                        JobPosting.duplicate_of_id,
                    )
                )
            ).mappings().all()
        targets = load_collection_targets()
        report = build_coverage_report(targets=targets, usable_rows=rows)
        report["recommended_batches"] = list(
            recommend_batches(targets=targets, coverage=report)
        )
        return report

    if args.commit:
        return await commit_collection_run(
            run_id=args.resume_run,
            collections_root=DEFAULT_COLLECTIONS_ROOT,
            database_url=ASYNC_DATABASE_URL,
            backup_dir=DEFAULT_BACKUP_DIR,
            confirm=args.confirm,
            authorization_manifest_path=args.authorization_manifest,
        )

    service = CollectionService(collections_root=DEFAULT_COLLECTIONS_ROOT)
    async with AsyncSessionLocal() as session:
        if args.resume_run:
            return await service.resume_dry_run(
                args.resume_run,
                db=session,
                authorization_manifest_path=args.authorization_manifest,
            )
        return await service.run_dry_run(
            source_ids=args.source,
            run_id=args.run_id,
            max_records=args.max_records,
            max_pages=args.max_pages,
            max_requests=args.max_requests,
            manifest_path=args.manifest,
            input_file_path=args.input_file,
            authorization_note=args.authorization_note,
            authorization_manifest_path=args.authorization_manifest,
            record_offset=args.record_offset,
            db=session,
        )


def _write_coverage_output(path: Path, payload: str) -> Path:
    root = (
        Path(__file__).resolve().parents[1] / "data" / "expansion-reports"
    ).resolve()
    target = path.resolve()
    if target.parent != root or target.suffix.casefold() != ".json":
        raise ValueError(
            "coverage output must be a JSON file directly under data/expansion-reports"
        )
    root.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(payload + "\n", encoding="utf-8")
    temporary.replace(target)
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(execute(args))
        payload = json.dumps(
            result, ensure_ascii=False, indent=2, sort_keys=True, default=str
        )
        if args.output:
            _write_coverage_output(args.output, payload)
    except (ValueError, PermissionError) as exc:
        if args.debug:
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except StorageError as exc:
        if args.debug:
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except (httpx.HTTPError, SourceStopped) as exc:
        if args.debug:
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 4
    except (DatabaseOperationalError, sqlite3.Error, SQLAlchemyError) as exc:
        if args.debug:
            raise
        detail = getattr(exc, "orig", exc)
        print(f"error: {detail}", file=sys.stderr)
        return 5
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
