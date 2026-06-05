"""
提案機会検知くん スモークテスト

samples/ の議事録サンプル9件を使って以下を確認する：
- 議事録パース（▼決定事項/▼タスク/▼メモ抽出）
- 社外/社内振り分け（コール名プレフィクス3段階判定）
- 期日抽出（A型確定日 / B型原文）
- 案件名・会議タイトル抽出

Slack/Claude API は呼ばない（環境変数なしで実行可能）。

実行：
    cd C:\\Users\\risaki_iha\\Repos\\Claim_check
    python scripts/smoke_test_teian_kenchi.py
"""

import sys
from datetime import datetime
from pathlib import Path

# Windows コンソールの cp932 を回避して絵文字を出せるようにする
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent))

# import 時に外部API依存が走らない関数だけを直接import
from lib.teian_kenchi import (
    parse_minutes_post,
    extract_due_date,
    classify_meeting_type,
    extract_project_name,
    strip_prefix_from_call_name,
)

SAMPLES_DIR = Path(r"C:\Users\risaki_iha\projects\teian-kenchi\samples")


def strip_frontmatter(content: str) -> tuple[dict, str]:
    """YAML フロントマター + 本文 を分離（簡易パーサ）"""
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    meta = {}
    for line in parts[1].strip().split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"')
    body = parts[2].strip()
    return meta, body


def make_post_dict(meta: dict, body: str) -> dict:
    """サンプルメタ+本文 → Slack post dict 形式に変換

    サンプル本文は「要約:」以降「関係構築・心理的距離:」直前を抽出済みの状態で
    保存されているため、parse_minutes_post が見つけられるよう前後にマーカーを擬似的に追加する。
    """
    posted_at = meta.get("posted_at", "")
    try:
        ts = datetime.fromisoformat(posted_at).timestamp()
    except Exception:
        ts = 0.0
    wrapped_text = f"要約:\n\n{body}\n\n関係構築・心理的距離:\n（このサンプルでは省略）"
    return {
        "text": wrapped_text,
        "ts": str(ts),
        "permalink": meta.get("permalink", ""),
        "channel": {
            "id": meta.get("channel_id", ""),
            "name": meta.get("channel_name", "").lstrip("#"),
        },
    }


def main() -> None:
    sample_files = sorted(SAMPLES_DIR.glob("*.md"))
    if not sample_files:
        print(f"❌ サンプル無し: {SAMPLES_DIR}")
        sys.exit(1)
    print(f"📂 found {len(sample_files)} samples in {SAMPLES_DIR}\n")

    type_correct = 0
    type_total = 0
    parse_failed = 0

    for sample in sample_files:
        print(f"\n{'=' * 78}")
        print(f"📄 {sample.name}")
        print("=" * 78)

        content = sample.read_text(encoding="utf-8")
        meta, body = strip_frontmatter(content)
        post = make_post_dict(meta, body)

        mtg = parse_minutes_post(post)
        if not mtg:
            print("  ❌ パース失敗（抽出範囲が見つからない）")
            parse_failed += 1
            continue

        mtype = classify_meeting_type(
            mtg.call_name, mtg.meeting_title, mtg.tasks_customer, mtg.memo
        )
        project = extract_project_name(mtg.channel_name, mtg.call_name)
        clean_title = strip_prefix_from_call_name(mtg.call_name)

        expected_type = meta.get("classification", "?")  # external / internal
        expected_jp = {"external": "社外", "internal": "社内"}.get(expected_type, expected_type)
        type_ok = (mtype == expected_jp)
        type_total += 1
        if type_ok:
            type_correct += 1

        print(f"  コール名      : {mtg.call_name}")
        print(f"  案件名        : {project}")
        print(f"  会議タイトル  : {clean_title}")
        print(f"  社外/社内     : {mtype}  期待: {expected_jp}  {'✅' if type_ok else '❌'}")
        print(f"  ▼決定事項     : {len(mtg.decisions)}件")
        for d in mtg.decisions[:3]:
            due, raw = extract_due_date(d)
            due_info = f"[A型: {due}]" if due else f"[B型: {raw}]" if raw else "[期日なし]"
            print(f"    ・ {d[:60]}{'...' if len(d) > 60 else ''} {due_info}")
        if len(mtg.decisions) > 3:
            print(f"    ...他{len(mtg.decisions) - 3}件")

        print(f"  ▼タスク<顧客> : {len(mtg.tasks_customer)}件")
        for t in mtg.tasks_customer[:3]:
            due, raw = extract_due_date(t)
            due_info = f"[A型: {due}]" if due else f"[B型: {raw}]" if raw else "[期日なし]"
            print(f"    ・ {t[:60]}{'...' if len(t) > 60 else ''} {due_info}")
        if len(mtg.tasks_customer) > 3:
            print(f"    ...他{len(mtg.tasks_customer) - 3}件")

        print(f"  ▼タスク<ナイル>: {len(mtg.tasks_nyle)}件")
        for t in mtg.tasks_nyle[:3]:
            due, raw = extract_due_date(t)
            due_info = f"[A型: {due}]" if due else f"[B型: {raw}]" if raw else "[期日なし]"
            print(f"    ・ {t[:60]}{'...' if len(t) > 60 else ''} {due_info}")
        if len(mtg.tasks_nyle) > 3:
            print(f"    ...他{len(mtg.tasks_nyle) - 3}件")

        print(f"  ▼メモ         : {len(mtg.memo)}件")

    print(f"\n{'=' * 78}")
    print("📊 結果サマリ")
    print("=" * 78)
    print(f"  サンプル数        : {len(sample_files)}")
    print(f"  パース失敗        : {parse_failed}")
    print(f"  社外/社内判定精度 : {type_correct}/{type_total}  ({100 * type_correct / max(type_total, 1):.0f}%)")


if __name__ == "__main__":
    main()
