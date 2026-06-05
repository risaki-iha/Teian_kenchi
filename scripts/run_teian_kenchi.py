"""
提案機会検知くん - GitHub Actions エントリポイント
"""

import sys
from pathlib import Path

# scripts/lib をインポート可能に
sys.path.insert(0, str(Path(__file__).parent))

from lib.teian_kenchi import run_teian_kenchi


if __name__ == "__main__":
    run_teian_kenchi()
