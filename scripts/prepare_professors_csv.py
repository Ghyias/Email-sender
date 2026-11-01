#!/usr/bin/env python3
"""Convert a verified China professors CSV into a MailPilot-ready import file."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from app.importers.china_professors import mailpilot_import_columns, normalize_professor_dataframe


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--qa", type=Path, default=None, help="Write QA JSON beside output")
    args = parser.parse_args()

    df = pd.read_csv(args.input_csv)
    ready, qa = normalize_professor_dataframe(df, split_batches=True)
    cols = [c for c in mailpilot_import_columns() if c in ready.columns]
    # keep helpful extras at the end
    extras = [c for c in ready.columns if c not in cols]
    ready[cols + extras].to_csv(args.output, index=False)
    qa_path = args.qa or args.output.with_suffix(".qa.json")
    qa_path.write_text(json.dumps(qa, indent=2), encoding="utf-8")
    print(f"Wrote {len(ready)} rows → {args.output}")
    print(json.dumps(qa, indent=2))


if __name__ == "__main__":
    main()
