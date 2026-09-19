"""导出服务端 OpenAPI 快照，并可与 Flutter 仓库的契约快照做比对/同步。

用法：
    python scripts/export_openapi.py                 # 只写本仓库 openapi.json
    python scripts/export_openapi.py --check         # 只比对，不写文件
    python scripts/export_openapi.py --sync          # 比对通过后写回 Flutter 仓库契约快照
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import contract_diff  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def dump(schema: dict) -> str:
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出/校验 OpenAPI 快照")
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "openapi.json", help="导出路径")
    parser.add_argument("--contract", type=Path, default=contract_diff.DEFAULT_CONTRACT, help="冻结契约路径")
    parser.add_argument("--check", action="store_true", help="只比对，不写文件")
    parser.add_argument("--sync", action="store_true", help="比对通过后写回冻结契约")
    args = parser.parse_args(argv)

    schema = contract_diff.server_schema()

    if not args.check:
        args.out.write_text(dump(schema), encoding="utf-8")
        print(f"已导出 {args.out}（{len(schema.get('paths', {}))} 条路径）")

    if not args.contract.exists():
        print(f"找不到契约文件，跳过比对：{args.contract}", file=sys.stderr)
        return 2 if args.sync else 0

    report = contract_diff.compare(contract_diff.load_json(args.contract), schema)
    for message in report.errors:
        print(f"[ERROR] {message}", file=sys.stderr)
    print(f"比对：失败 {len(report.errors)} 项，警告 {len(report.warnings)} 项")

    if args.sync:
        if report.errors:
            print("比对未通过，拒绝同步以保护冻结契约", file=sys.stderr)
            return 1
        args.contract.write_text(dump(schema), encoding="utf-8")
        print(f"已同步契约快照 {args.contract}")
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
