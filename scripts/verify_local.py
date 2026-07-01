# -*- coding: utf-8 -*-
"""ローカル検証専用スクリプト（CI からは呼ばない）。

指定した amptalk call_id を、本番と同じ実コード経路
（build_meeting_from_call → generate_distinct_items）に通して、
💰/🚨/● の判定結果だけを表示する。
Slack 投稿・スプシ書き込み・重複排除（スプシ L 列参照）は一切通らない。

★重要（トークンを食わない設計）★
   本スクリプトは Anthropic API key 経路でのみ動く。
   本番 bot が依存する OAuth リフレッシュトークン（CLAUDE_REFRESH_TOKEN）には
   一切触れない。冒頭で明示的に空にして OAuth を封じているため、
   ローカル検証でトークンがローテーション／失効するリスクがない。

使い方:
  1. リポ直下に .env を用意し、次の3つを書く（.env は .gitignore 済み）:
       ANTHROPIC_API_KEY=sk-ant-...     # Anthropic Console で発行（OAuth枠とは別の従量課金）
       AMPTALK_API_KEY=...              # amptalk 配布キットの .env からコピー
       AMPTALK_AUTH_KEY=...             # amptalk 配布キットの .env からコピー
  2. python scripts/verify_local.py <call_id> [<call_id> ...]

  amptalk URL（https://amptalk.net/calls/detail?id=<UUID>）の <UUID> が call_id。
"""

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENV_PATH = REPO / ".env"


def _load_env(path: Path) -> None:
    """.env を os.environ に流し込む（既存の環境変数は上書きしない）。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_env(ENV_PATH)

# ★安全弁：OAuth を封じて本番トークンを絶対に消費しない（api_key 経路のみで動かす）
os.environ["CLAUDE_REFRESH_TOKEN"] = ""

if not os.environ.get("ANTHROPIC_API_KEY"):
    sys.exit(
        "[verify_local] ANTHROPIC_API_KEY がありません。リポ直下の .env に入れてください。\n"
        "  （本スクリプトは OAuth を使わない＝本番トークンを食わない設計です）"
    )
if not (os.environ.get("AMPTALK_API_KEY") and os.environ.get("AMPTALK_AUTH_KEY")):
    sys.exit(
        "[verify_local] AMPTALK_API_KEY / AMPTALK_AUTH_KEY がありません。\n"
        "  amptalk 配布キットの .env からリポ直下の .env にコピーしてください。"
    )

sys.path.insert(0, str(Path(__file__).parent))
from lib.claude_oauth import ClaudeClient  # noqa: E402
from lib.teian_kenchi import (  # noqa: E402
    build_meeting_from_call,
    generate_distinct_items,
)

SKILL_PATH = REPO / "skills" / "teian-kenchi-realtime.md"


def main(call_ids: list[str]) -> None:
    if not call_ids:
        sys.exit("使い方: python scripts/verify_local.py <call_id> [<call_id> ...]")

    skill_content = SKILL_PATH.read_text(encoding="utf-8")
    claude = ClaudeClient()  # CLAUDE_REFRESH_TOKEN 空 → api_key 経路で動く

    for raw in call_ids:
        call_id = raw.strip().lower()
        mtg = build_meeting_from_call({"id": call_id})
        if not mtg:
            print(f"\n===== {call_id[:8]}… : 取得/パース失敗（解析未完了・要約空など）=====")
            continue

        items = generate_distinct_items(
            claude,
            mtg,
            skill_content,
            meeting_type="社外",
            project=mtg.call_name,
            clean_title=mtg.call_name,
        )
        print(f"\n===== {call_id[:8]}… {mtg.call_name} =====")
        if not items:
            print("  （検知項目なし）")
        for it in items:
            summ = it.summary[:50] + ("…" if len(it.summary) > 50 else "")
            due = f" 【期日】{it.due_raw}" if it.due_raw else ""
            print(f"  {it.emoji} [{it.category}] {it.label} — {summ}{due}")


if __name__ == "__main__":
    main(sys.argv[1:])
