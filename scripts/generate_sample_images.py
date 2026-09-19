"""生成本地示例图片。

用法：python scripts/generate_sample_images.py [--force] [--verbose]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.fixtures.images import STATIC_DIR, ensure_sample_images  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="生成本地示例图片")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    parser.add_argument("--verbose", action="store_true", help="打印每个文件")
    args = parser.parse_args()

    written = ensure_sample_images(force=args.force, verbose=args.verbose)
    total = sum(1 for _ in STATIC_DIR.glob("*.jpg")) if STATIC_DIR.exists() else 0
    print(f"本次写入 {written} 个文件，目录现有 {total} 个文件：{STATIC_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
