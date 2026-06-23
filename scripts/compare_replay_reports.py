"""Compare two replay reports and write a readable gate artifact.

Examples:
    python scripts/compare_replay_reports.py baseline.json candidate.json
    python scripts/compare_replay_reports.py baseline.json candidate.json --output diff.json --markdown diff.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from eidolon_agent.app.replay import (
    compare_replay_reports,
    load_report,
    render_comparison_markdown,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--markdown", type=Path, default=None)
    parser.add_argument("--max-first-delta-regression-ms", type=int, default=150)
    parser.add_argument("--max-total-regression-ms", type=int, default=250)
    args = parser.parse_args()

    comparison = compare_replay_reports(
        load_report(args.baseline),
        load_report(args.candidate),
        max_first_delta_regression_ms=args.max_first_delta_regression_ms,
        max_total_regression_ms=args.max_total_regression_ms,
    )
    payload = comparison.to_metadata()
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
    if args.output is not None:
        output = args.output.expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(f"wrote replay comparison to {output}")
    else:
        print(text)

    markdown = args.markdown.expanduser() if args.markdown is not None else None
    if markdown is None and args.output is not None:
        markdown = args.output.expanduser().with_suffix(".md")
    if markdown is not None:
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markdown.write_text(render_comparison_markdown(comparison), encoding="utf-8")
        print(f"wrote readable comparison to {markdown}")
    return 0 if comparison.passed else 1


if __name__ == "__main__":
    sys.exit(main())
