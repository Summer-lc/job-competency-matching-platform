from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from src.synthetic_resume_dataset import DEFAULT_SEED, write_dataset


DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "data" / "synthetic_resumes"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate deterministic synthetic resumes and benchmark fixtures."
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = write_dataset(args.output_dir, seed=args.seed)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
