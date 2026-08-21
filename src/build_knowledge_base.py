from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from model_class.base import Base
import model_class.job_competency  # noqa: F401
import model_class.knowledge_base  # noqa: F401
from src.import_service import import_job_file
from src.job_analysis_service import graph_data, rebuild_analysis
from src.knowledge_service import update_knowledge_chunks


@dataclass(frozen=True)
class BuildResult:
    report_path: Path
    graph_path: Path
    summary: dict


async def build_knowledge_base(
    input_path: Path, data_dir: Path | None = None
) -> BuildResult:
    input_path = input_path.resolve()
    if not input_path.is_file():
        raise FileNotFoundError(f"数据文件不存在: {input_path}")
    target_dir = (data_dir or Path(__file__).resolve().parents[1] / "data").resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    database_path = target_dir / "job_competency.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with Session() as session:
            summary = await import_job_file(
                session, input_path.read_bytes(), input_path.name
            )
            affected = set(summary.get("affected_families") or [])
            if affected and not summary.get("idempotent"):
                analysis = await rebuild_analysis(session, family_codes=affected)
                knowledge = await update_knowledge_chunks(session, affected)
            else:
                analysis = {
                    "profiles_created": 0,
                    "review_items_created": 0,
                    "families": 0,
                    "unchanged_families": sorted(affected),
                }
                knowledge = {"created": 0, "updated": 0, "families": []}
            graph = await graph_data(session, include_evidence=True)
    finally:
        await engine.dispose()

    summary = {**summary, "analysis": analysis, "knowledge": knowledge}
    reports_dir = target_dir / "imports"
    exports_dir = target_dir / "exports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    exports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{summary['batch_id']}-report.json"
    graph_path = exports_dir / "knowledge-graph.json"
    if not report_path.exists():
        report_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    graph_path.write_text(
        json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return BuildResult(report_path=report_path, graph_path=graph_path, summary=summary)


def main() -> None:
    parser = argparse.ArgumentParser(description="构建可持续更新的岗位知识库与知识图谱")
    parser.add_argument("input_path", type=Path, help="JSON、JSONL或CSV岗位数据文件")
    parser.add_argument("--data-dir", type=Path, default=None, help="数据库与报告输出目录")
    args = parser.parse_args()
    result = asyncio.run(build_knowledge_base(args.input_path, args.data_dir))
    print(json.dumps(result.summary, ensure_ascii=False, indent=2))
    print(f"report={result.report_path}")
    print(f"graph={result.graph_path}")


if __name__ == "__main__":
    main()
