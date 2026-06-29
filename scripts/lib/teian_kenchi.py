"""
提案機会検知くん コア実装（2026-06-09 改訂版v2・議事録単位フォーマット）

設計方針（v2.1・議事録単位 distinct 生成）:
- amptalk コールを直駆動し、要約minute＋ネイティブ提案機会検知を材料にする
- **議事録を丸ごと AI に渡し、重複を排した distinct な 機会(💰)/リスク(🚨)/タスク(●) を生成**
  （旧版は箇条書き1行=1項目で独立判定し過分割していた＝案Aで根治）
- 絵文字は3マーク: 💰 アップセル機会 / 🚨 競争・離反リスク / ● 普通のタスク
  （早急さは🏃マークにせず【期日】行で表現）
- **1議事録 = 1親メッセージ**（v2.2＝💰/🚨/● を領域カテゴリでグルーピングして整形）
- **1検知項目 = 1スプシ行**
- 💰 か 🚨 が1件でもある議事録だけ通知（● だけの議事録は完全スルー）
- ● タスクは期日付きのみ採用（期日なし● は通知もスプシも完全除外）
- メンションなし通知（マネ＋AMメンションは別フェーズ）
"""

import html
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .claude_oauth import ClaudeClient
from .slack_tools import SlackTools
from .teian_sheets_tools import TeianSheetsTools
from .amptalk_client import (
    fetch_calls,
    fetch_analysis,
    is_external_title,
    extract_call_id,
    call_detail_url,
)

JST = timezone(timedelta(hours=9))

# amptalk コール取得の遡り窓（時間）。毎回この窓を再走査し、重複排除で冪等にする。
# 解析未完了のコールを次回以降に拾い直すため、ポーリング間隔より十分広く取る。
LOOKBACK_HOURS = int(os.environ.get("AMPTALK_LOOKBACK_HOURS", "30"))
JP_WEEKDAYS = "月火水木金土日"

# 議事録転送Bot
MINUTES_BOT_USER_ID = "U0B305165M1"

# 通知先チャンネル
NOTIFICATION_CHANNEL = "C0AHUC1VDDK"  # #dxm_提案機会_検知くん

# 議事録パース用マーカー（柔軟化）
# 「要約:」があれば その以降を、なければ投稿全体を対象
EXTRACT_START_MARKERS = ["要約:", "要約：", "<会議議題>", "<議題>"]
EXTRACT_END_MARKERS = ["関係構築・心理的距離:", "関係構築・心理的距離："]

# 絵文字体系（v2.1・3マーク）: 💰 アップセル機会 / 🚨 競争・離反リスク / ● 普通のタスク
EMOJI_UPSELL = "💰"
EMOJI_RISK = "🚨"
EMOJI_TASK = "●"

# 並べ替え順（左ほど上）。AI出力の妥当な絵文字集合でもある
EMOJI_PRIORITY = [EMOJI_UPSELL, EMOJI_RISK, EMOJI_TASK]
VALID_EMOJI = set(EMOJI_PRIORITY)

# この絵文字が1件でもある議事録だけ通知する（● だけの議事録は完全スルー＝emoji0仕様）
NOTIFY_TRIGGER_EMOJI = {EMOJI_UPSELL, EMOJI_RISK}

# AI が返す source_section の妥当値（顧客タスク<顧客>は素材から除外済み）
VALID_SECTIONS = {"decision", "task_nyle", "bant", "memo"}

# セクション見出し
SECTION_LABEL_FOR_SHEET = {
    "decision": "決定事項",
    "task_customer": "タスク<顧客>",
    "task_nyle": "タスク<ナイル>",
    "bant": "BANT",
    "memo": "メモ",
}

# 領域カテゴリ（v2.2・固定7枠・この順で親メッセージに表示。出番ゼロの枠は非表示）
# AI が各項目に付与し、親メッセージは「領域 → 💰/🚨/● 優先度順」でグルーピングする
CATEGORY_ORDER = ["広告", "SEO", "サイト改善", "コンテンツ", "SNS運用", "計測・データ基盤", "その他"]
VALID_CATEGORIES = set(CATEGORY_ORDER)
CATEGORY_FALLBACK = "その他"

# amptalk議事録の冒頭にある :link: amptalk: <URL> 行を除去する正規表現
AMPTALK_LINK_PATTERN = re.compile(r"^:link:\s*amptalk:\s*<[^>]+>\s*\n?", re.MULTILINE)


@dataclass
class MinutesMeeting:
    """議事録投稿1件を構造化したもの"""
    posted_at: datetime
    channel_id: str
    channel_name: str
    permalink: str
    call_name: str
    meeting_title: str
    decisions: list[str] = field(default_factory=list)
    tasks_customer: list[str] = field(default_factory=list)
    tasks_nyle: list[str] = field(default_factory=list)
    bant: list[str] = field(default_factory=list)
    memo: list[str] = field(default_factory=list)
    # v2（amptalk文字起こし駆動）で追加
    call_id: str = ""        # amptalk call_id（UUID）。重複排除キー＆議事録URLの素
    proposal: str = ""       # amptalk ネイティブ「提案機会検知」minute（AI判定の文脈）
    customer: str = ""       # amptalk /calls の customer フィールド（案件名候補）


@dataclass
class DetectedItem:
    """検知された1項目（=スプシ1行）。Slack通知は議事録単位で集約される"""
    meeting_posted_at: datetime
    meeting_type: str            # 社外/社内
    channel_name: str
    channel_id: str
    project_name: str
    meeting_title: str
    emoji: str                   # 💰 / 🚨 / ● のいずれか
    label: str                   # テーマ見出し（8〜12字、【】記号なし）
    summary: str
    due_date: str                # YYYY-MM-DD or ""
    due_raw: str                 # 議事録上の原文
    minutes_url: str             # 議事録Bot投稿permalink
    source_section: str          # 'decision' | 'task_customer' | 'task_nyle' | 'bant' | 'memo'
    original_text: str
    category: str = ""           # 領域（広告/SEO/サイト改善/コンテンツ/SNS運用/計測・データ基盤/その他）
    notification_url: str = ""   # 親メッセージ投稿後に埋める
    meeting_key: str = ""        # 議事録単位の集約キー


# ========== メイン ==========

def run_teian_kenchi() -> None:
    """提案機会検知くん v2（amptalk文字起こし駆動）本体

    旧版は議事録転送Botの投稿をSlackからスキャンしてBot要約を検知材料にしていた。
    v2は amptalk API を直駆動し、コールの「文字起こし＋構造化要約(要約minute)＋
    ネイティブ提案機会検知minute」を材料に検知する。Slack投稿には依存しない。
    """
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    now_jst = datetime.now(JST)
    if event == "schedule" and (now_jst.hour < 10 or now_jst.hour >= 21):
        print(f"[skip] 営業時間外 ({now_jst.strftime('%H:%M')} JST)", flush=True)
        return

    claude = ClaudeClient()
    try:
        slack = SlackTools()
        sheets = TeianSheetsTools()
        skill_path = Path(__file__).parent.parent.parent / "skills" / "teian-kenchi-realtime.md"
        skill_content = skill_path.read_text(encoding="utf-8")

        # Phase 1: 取得窓（callDatetime）を決定
        after_dt, before_dt = determine_call_window()
        print(f"[range] {after_dt:%Y/%m/%d %H:%M} 〜 {before_dt:%Y/%m/%d %H:%M} JST", flush=True)

        # Phase 2: amptalk からコール取得 → 社外フィルタ（title プレフィクス）
        calls = fetch_calls(after_dt, before_dt)
        external = [c for c in calls if is_external_title(c.get("title", ""))]
        print(f"[scan] {len(calls)} calls / {len(external)} external", flush=True)

        # Phase 3: 既処理 call_id をスプシ L列（議事録URL=amptalk URL）から引いて重複排除
        seen = {extract_call_id(u) for u in sheets.existing_minutes_urls()}
        seen.discard("")
        targets = [c for c in external if str(c.get("id", "")).lower() not in seen]
        print(f"[dedup] new={len(targets)} (skipped {len(external) - len(targets)})", flush=True)

        if not targets:
            _post_heartbeat(slack, after_dt, before_dt)
            print("[end] 新規社外コール0件、終了", flush=True)
            return

        # Phase 4: analysis 取得 → 議事録構築（要約minuteをパース）→ 項目展開
        meetings: list[MinutesMeeting] = []
        for c in targets:
            mtg = build_meeting_from_call(c)
            if mtg:
                meetings.append(mtg)
        print(f"[parse] {len(meetings)} meetings built", flush=True)

        if not meetings:
            _post_heartbeat(slack, after_dt, before_dt)
            print("[end] 解析未完了/空、終了", flush=True)
            return

        items: list[DetectedItem] = []
        meeting_lookup: dict[str, MinutesMeeting] = {}

        # Phase 5: 議事録を丸ごと AI に渡し、重複を排した distinct な
        # 機会(💰)/リスク(🚨)/タスク(●) を生成する（過分割の根治＝案A）。
        # 顧客タスク（<顧客>）は素材に含めない（6/18決定）。
        for mtg in meetings:
            mtype = classify_meeting_type(mtg.call_name, mtg.meeting_title, mtg.tasks_customer, mtg.memo)
            # 社内MTGは完全スキップ（社外フィルタ済みだが二重で守る）
            if mtype == "社内":
                print(f"[skip] 社内MTG: {mtg.call_name}", flush=True)
                continue
            project = mtg.customer.strip() or extract_project_name(mtg.channel_name, mtg.call_name)
            clean_title = strip_prefix_from_call_name(mtg.call_name)
            meeting_lookup[mtg.call_id] = mtg

            distinct = generate_distinct_items(
                claude, mtg, skill_content,
                meeting_type=mtype, project=project, clean_title=clean_title,
            )
            items.extend(distinct)
            print(f"[distinct] {clean_title[:24]}…: {len(distinct)} items", flush=True)

        print(f"[evaluate] {len(items)} distinct items（顧客タスク除外・議事録内重複排除済み）", flush=True)

        # Phase 6: 議事録ごとにグループ化 → 💰/🚨 トリガー判定 → 通知＆スプシ追記
        grouped: dict[str, list[DetectedItem]] = {}
        for it in items:
            grouped.setdefault(it.meeting_key, []).append(it)

        dry_run = _is_dry_run()
        if dry_run:
            print("[dry-run] Slack投稿・スプシ書き込みは行わない（ログのみ）", flush=True)

        rows_to_append: list[dict] = []
        posted_meetings = 0
        for meeting_key, group_items in grouped.items():
            mtg = meeting_lookup.get(meeting_key)
            if not mtg:
                continue

            # 💰 か 🚨 が1つも無い議事録（● だけ）は親投稿もスプシも完全スルー
            # （アップセル機会もリスクも無い＝マネ＋AMに上げる価値なし）
            if not any(i.emoji in NOTIFY_TRIGGER_EMOJI for i in group_items):
                print(f"[skip-no-trigger] meeting={meeting_key} 💰/🚨 が0件のため完全スルー", flush=True)
                continue

            parent_text = build_parent_message(mtg, group_items)

            # dry-run：投稿せずログ出力、行は組み立てるが追記しない
            if dry_run:
                print(f"[dry-run][parent] meeting={meeting_key}\n{parent_text}\n", flush=True)
                for it in group_items:
                    it.notification_url = "(dry-run)"
                    rows_to_append.append(build_sheet_row(it))
                posted_meetings += 1
                continue

            # 親メッセージ投稿（セクション内を絵文字優先順位順に並べ替え済み）
            parent_resp = slack.post_message(NOTIFICATION_CHANNEL, parent_text)
            parent_ts = parent_resp.get("ts", "") if parent_resp.get("ok") else ""
            parent_permalink = (
                slack_get_permalink(slack, NOTIFICATION_CHANNEL, parent_ts)
                if parent_ts else ""
            )

            # 全項目の notification_url に親メッセージpermalink を入れる
            for it in group_items:
                it.notification_url = parent_permalink
                rows_to_append.append(build_sheet_row(it))
            posted_meetings += 1
            print(f"[notify] meeting={meeting_key} items={len(group_items)} posted", flush=True)

        if not rows_to_append:
            _post_heartbeat(slack, after_dt, before_dt)
            print("[notify] 全議事録がemoji0スルー、ハートビートのみ投稿", flush=True)
            return

        if dry_run:
            print(f"[dry-run] would append {len(rows_to_append)} rows（{posted_meetings} meetings）。スプシ書き込みスキップ", flush=True)
            return

        appended = sheets.append_rows(rows_to_append)
        print(f"[sheets] appended {appended} rows（{posted_meetings} meetings）", flush=True)
    finally:
        if claude.has_token_rotated():
            _emit_refresh_token_output(claude.get_current_refresh_token())


def _is_dry_run() -> bool:
    return os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def _post_heartbeat(slack: SlackTools, after_dt: datetime, before_dt: datetime) -> None:
    """検知0件でも稼働確認のため「💤 検知なし」をハートビート投稿する（将来オフ化予定）"""
    if _is_dry_run():
        print("[dry-run] heartbeat 投稿スキップ", flush=True)
        return
    text = f"💤 検知なし\n対象範囲: {after_dt:%Y/%m/%d %H:%M} 〜 {before_dt:%Y/%m/%d %H:%M} JST"
    try:
        slack.post_message(NOTIFICATION_CHANNEL, text)
        print("[heartbeat] 検知0件のハートビート投稿", flush=True)
    except Exception as e:
        print(f"[heartbeat error] {e}", flush=True)


def _emit_refresh_token_output(token: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    print(f"::add-mask::{token}", flush=True)
    with open(output_path, "a", encoding="utf-8") as f:
        f.write(f"new_refresh_token={token}\n")
    print("[oauth] 🔁 GITHUB_OUTPUT に new_refresh_token を書き出した", flush=True)


# ========== Phase 1: 取得窓 ==========

def determine_call_window() -> tuple[datetime, datetime]:
    """amptalk /calls の callDatetime 取得窓 (after, before) を返す。

    - CUSTOM_AFTER / CUSTOM_BEFORE（"YYYY-MM-DD HH:MM" JST）があればそれを使う（手動再実行用）
    - 通常は [now - LOOKBACK_HOURS, now]。窓を毎回再走査し、重複排除（L列call_id）で冪等化。
      解析未完了のコールも、次回以降この窓に入っている限り拾い直せる。
    """
    custom_after = (os.environ.get("CUSTOM_AFTER") or "").strip()
    custom_before = (os.environ.get("CUSTOM_BEFORE") or "").strip()
    if custom_after and custom_before:
        af = datetime.strptime(custom_after, "%Y-%m-%d %H:%M").replace(tzinfo=JST)
        bf = datetime.strptime(custom_before, "%Y-%m-%d %H:%M").replace(tzinfo=JST)
        return af, bf

    now = datetime.now(JST)
    return now - timedelta(hours=LOOKBACK_HOURS), now


# ========== Phase 4: コール → 議事録構築 ==========

def build_meeting_from_call(call: dict) -> MinutesMeeting | None:
    """amptalk コール1件から analysis を取得し、要約minuteをパースして
    MinutesMeeting を構築する。解析未完了・要約空・パース全空は None。
    """
    call_id = str(call.get("id", "")).lower()
    if not call_id:
        return None

    analysis = fetch_analysis(call_id)
    if not analysis or not analysis.get("is_finished"):
        print(f"[parse] 解析未完了/取得不可: {call_id[:8]}…", flush=True)
        return None

    summary = analysis.get("summary", "")
    if not summary.strip():
        print(f"[parse] 要約minute空: {call_id[:8]}…", flush=True)
        return None

    # 要約minute は Slack議事録Bot投稿と同形式（<会議議題>…▼決定事項…▼タスク…）
    body = extract_target_range_flex(summary)
    if not body.strip():
        return None

    meeting_title = extract_meeting_title(body)
    decisions = extract_section_lines(body, "▼決定事項")
    tasks_block = extract_section_block(body, "▼タスク")
    tasks_customer = extract_subsection_lines(tasks_block, "<顧客>")
    tasks_nyle = extract_subsection_lines(tasks_block, "<ナイル>")
    bant = extract_section_lines(body, "▼BANT")
    memo = extract_section_lines(body, "▼メモ")

    if not any([decisions, tasks_customer, tasks_nyle, bant, memo]):
        return None

    try:
        posted_at = datetime.fromisoformat(call.get("callDatetime", "")).astimezone(JST)
    except (ValueError, TypeError):
        posted_at = datetime.now(JST)

    return MinutesMeeting(
        posted_at=posted_at,
        channel_id="",
        channel_name="",
        permalink=call_detail_url(call_id),
        call_name=str(call.get("title", "")),
        meeting_title=meeting_title,
        decisions=decisions,
        tasks_customer=tasks_customer,
        tasks_nyle=tasks_nyle,
        bant=bant,
        memo=memo,
        call_id=call_id,
        proposal=analysis.get("proposal", ""),
        customer=str(call.get("customer", "")),
    )


# ========== Phase 2: スキャン ==========

def scan_minutes_bot_posts(slack: SlackTools, after_ts: int, before_ts: int) -> list[dict]:
    return _search_bot_messages(slack, MINUTES_BOT_USER_ID, after_ts, before_ts, limit=50)


def _search_bot_messages(slack: SlackTools, bot_user_id: str, after_ts: int, before_ts: int, limit: int = 50) -> list[dict]:
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    import time as _time

    def _ymd(ts: int, offset_days: int = 0) -> str:
        return (_dt.fromtimestamp(ts, tz=_tz(_td(hours=9))) + _td(days=offset_days)).strftime("%Y-%m-%d")

    full_query = f"from:<@{bot_user_id}> after:{_ymd(after_ts, -1)} before:{_ymd(before_ts, 1)}"

    try:
        resp = slack.search_client.search_messages(query=full_query, count=limit, sort="timestamp")
    except Exception as e:
        print(f"[scan error] {e}", flush=True)
        return []

    _time.sleep(1.0)
    matches = resp.get("messages", {}).get("matches", []) or []
    return [m for m in matches if after_ts <= float(m.get("ts", 0)) < before_ts]


# ========== Phase 3: 議事録パース ==========

def parse_minutes_post(post: dict) -> MinutesMeeting | None:
    # Slack search.messages API は本文を HTMLエスケープして返すため、ここで復元する
    # （&lt;顧客&gt; → <顧客>、&amp; → & 等。extract_subsection_lines や
    #  extract_meeting_title が <顧客>/<ナイル>/<会議議題> を正しくマッチするために必要）
    text = html.unescape(post.get("text", ""))

    # amptalk議事録の冒頭にある「:link: amptalk: <URL>」行は通知に不要なので除去
    text = AMPTALK_LINK_PATTERN.sub("", text).strip()

    # 抽出範囲を柔軟に決定（「要約:」など開始マーカーがあればそこから、なければ全体）
    body = extract_target_range_flex(text)
    if not body.strip():
        return None

    call_name = extract_call_name(text)
    meeting_title = extract_meeting_title(body)

    decisions = extract_section_lines(body, "▼決定事項")
    tasks_block = extract_section_block(body, "▼タスク")
    tasks_customer = extract_subsection_lines(tasks_block, "<顧客>")
    tasks_nyle = extract_subsection_lines(tasks_block, "<ナイル>")
    bant = extract_section_lines(body, "▼BANT")
    memo = extract_section_lines(body, "▼メモ")

    # どのセクションも空の場合は議事録として扱わない
    if not any([decisions, tasks_customer, tasks_nyle, bant, memo]):
        return None

    ch = post.get("channel", {})
    channel_id = ch.get("id", "") if isinstance(ch, dict) else ""
    channel_name = ch.get("name", "") if isinstance(ch, dict) else ""

    try:
        posted_at = datetime.fromtimestamp(float(post.get("ts", "0")), tz=JST)
    except (ValueError, TypeError):
        posted_at = datetime.now(JST)

    return MinutesMeeting(
        posted_at=posted_at,
        channel_id=channel_id,
        channel_name=channel_name,
        permalink=post.get("permalink", ""),
        call_name=call_name,
        meeting_title=meeting_title,
        decisions=decisions,
        tasks_customer=tasks_customer,
        tasks_nyle=tasks_nyle,
        bant=bant,
        memo=memo,
    )


def extract_target_range_flex(text: str) -> str:
    """抽出範囲を柔軟に決定。

    - 開始マーカー（「要約:」「<会議議題>」「<議題>」など）が見つかればそこ以降
    - 見つからなければ text 全体を返す
    - 終了マーカー（「関係構築・心理的距離:」）が見つかればその直前で打ち切り
    """
    start_idx = 0
    for marker in EXTRACT_START_MARKERS:
        idx = text.find(marker)
        if idx != -1:
            start_idx = idx
            break

    end_idx = len(text)
    for marker in EXTRACT_END_MARKERS:
        idx = text.find(marker, start_idx)
        if idx != -1:
            end_idx = idx
            break

    return text[start_idx:end_idx].strip()


def extract_call_name(text: str) -> str:
    m = re.search(r"コール名[:：]\s*(.+)", text)
    if m:
        return m.group(1).strip()
    first_line = text.strip().split("\n", 1)[0]
    return first_line.strip()


def extract_meeting_title(body: str) -> str:
    """<会議議題> or <議題> の本文（次の ▼ や < 直前まで）"""
    for pattern in [
        r"<会議議題>\s*\n(.+?)(?=\n▼|\n<|\Z)",
        r"<議題>\s*\n(.+?)(?=\n▼|\n<|\Z)",
    ]:
        m = re.search(pattern, body, re.DOTALL)
        if m:
            return m.group(1).strip()
    return ""


def extract_section_lines(body: str, section_marker: str) -> list[str]:
    """▼決定事項 / ▼メモ / ▼BANT などの箇条書きを抽出"""
    pattern = re.escape(section_marker) + r"\s*\n(.+?)(?=\n▼|\Z)"
    m = re.search(pattern, body, re.DOTALL)
    if not m:
        return []
    return _extract_bullet_lines(m.group(1))


def extract_section_block(body: str, section_marker: str) -> str:
    pattern = re.escape(section_marker) + r"\s*\n(.+?)(?=\n▼|\Z)"
    m = re.search(pattern, body, re.DOTALL)
    return m.group(1) if m else ""


def extract_subsection_lines(block: str, sub_marker: str) -> list[str]:
    pattern = re.escape(sub_marker) + r"\s*\n(.+?)(?=\n<|\Z)"
    m = re.search(pattern, block, re.DOTALL)
    if not m:
        return []
    return _extract_bullet_lines(m.group(1))


def _extract_bullet_lines(text: str) -> list[str]:
    """・ で始まる箇条書きを行ごとに抽出。
    複数行にまたがる項目（インデントされた続き行）も結合する。
    """
    items: list[str] = []
    current: list[str] = []
    for raw in text.split("\n"):
        stripped = raw.strip().lstrip("　 ")
        if stripped.startswith("・"):
            # 直前の項目を確定
            if current:
                joined = " ".join(current).strip()
                if joined and joined not in ["特になし", "特段なし", "なし", "ー", "-"]:
                    items.append(joined)
            content = stripped.lstrip("・").strip()
            current = [content] if content else []
        elif stripped and current:
            # インデント続き行を結合（└─・- など）
            cleaned = stripped.lstrip("└─-").strip()
            if cleaned:
                current.append(cleaned)
    if current:
        joined = " ".join(current).strip()
        if joined and joined not in ["特になし", "特段なし", "なし", "ー", "-"]:
            items.append(joined)
    return items


# ========== Phase 4: 社外/社内振り分け & 案件名抽出 ==========

def classify_meeting_type(call_name: str, title: str, tasks_customer: list[str], memo: list[str]) -> str:
    if any(x in call_name for x in ["【社外】", "社外_", "【外部】"]):
        return "社外"
    if any(x in call_name for x in ["【社内】", "社内_", "【内部】"]):
        return "社内"
    if "【確定】" in call_name or "【Zoom】" in call_name:
        return "社外"
    if tasks_customer:
        return "社外"
    return "社内"


def extract_project_name(channel_name: str, call_name: str) -> str:
    m = re.match(r"社内_(.+?)(?:_[\d\-]+)?$", channel_name)
    if m:
        name = re.sub(r"_[\d\-]+$", "", m.group(1))
        return name
    return strip_prefix_from_call_name(call_name)


def strip_prefix_from_call_name(call_name: str) -> str:
    cleaned = call_name
    # 通常の議事録Bot投稿のコール名プレフィクス + amptalk議事録の <会議議題>/<議題> ラベル
    for prefix in [
        "【社外】", "【社内】", "【確定】", "【外部】", "【内部】", "【Zoom】",
        "社外_", "社内_",
        "<会議議題>", "<議題>",
    ]:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
    return cleaned.strip()


# ========== 期日抽出 ==========

DUE_DATE_REGEX = re.compile(r"(20\d{2})/(\d{1,2})/(\d{1,2})")
DUE_BRACKET_REGEX = re.compile(
    r"[（(]([^（）()]*?(?:期日|〆|まで|までに|予定|末|上旬|中旬|下旬|早急|随時|進行中|未定|指定なし|完了時|受領後|以内|想定|XX|月中|/中)[^（）()]*?)[）)]"
)


def extract_due_date(text: str) -> tuple[str, str]:
    bracket_match = DUE_BRACKET_REGEX.search(text)
    if not bracket_match:
        dates = DUE_DATE_REGEX.findall(text)
        if dates:
            latest = max(dates, key=lambda d: (int(d[0]), int(d[1]), int(d[2])))
            try:
                return f"{latest[0]}-{int(latest[1]):02d}-{int(latest[2]):02d}", ""
            except (ValueError, TypeError):
                pass
        return "", ""

    raw = bracket_match.group(0)
    inner = bracket_match.group(1)

    dates = DUE_DATE_REGEX.findall(inner)
    if dates:
        latest = max(dates, key=lambda d: (int(d[0]), int(d[1]), int(d[2])))
        try:
            normalized = f"{latest[0]}-{int(latest[1]):02d}-{int(latest[2]):02d}"
            return normalized, raw
        except (ValueError, TypeError):
            pass

    return "", raw


# ========== Phase 5: 議事録単位の distinct 生成 ==========

def generate_distinct_items(
    claude: ClaudeClient,
    mtg: MinutesMeeting,
    skill_content: str,
    meeting_type: str,
    project: str,
    clean_title: str,
) -> list[DetectedItem]:
    """議事録1件を丸ごと AI に渡し、重複を排した distinct な検知項目を得る（案A）。

    旧版は箇条書き1行=1項目を独立判定していたため、amptalk要約が同じ論点を
    ▼決定/▼タスク/▼メモ に繰り返すと同じ機会が逐語重複した（過分割）。
    v2.1 は議事録単位で AI に重複排除させ、distinct な 💰/🚨/● だけ返す。
    """
    blob = _build_meeting_blob(mtg)
    if not blob.strip():
        return []

    results = _request_distinct(claude, blob, skill_content)

    items: list[DetectedItem] = []
    for r in results:
        if not isinstance(r, dict) or r.get("is_noise", False):
            continue
        summary = (r.get("summary", "") or "").strip()
        if not summary:
            continue
        emoji = _normalize_emoji(r.get("emoji", ""))
        due_in = (r.get("due", "") or "").strip()
        # v2.2: ● タスクは期日付きのみ採用。期日なし● は通知もスプシも完全除外
        if emoji == EMOJI_TASK and not due_in:
            continue
        due_date, _ = extract_due_date(due_in) if due_in else ("", "")
        items.append(DetectedItem(
            meeting_posted_at=mtg.posted_at,
            meeting_type=meeting_type,
            channel_name=mtg.channel_name,
            channel_id=mtg.channel_id,
            project_name=project,
            meeting_title=clean_title,
            emoji=emoji,
            label=(r.get("label", "") or "").strip(),
            summary=summary,
            due_date=due_date,
            due_raw=due_in,
            minutes_url=mtg.permalink,
            source_section=_normalize_section(r.get("source_section", "")),
            category=_normalize_category(r.get("category", "")),
            original_text="",
            meeting_key=mtg.call_id,
        ))
    return items


def _build_meeting_blob(mtg: MinutesMeeting) -> str:
    """議事録1件を AI 判定用の1つのテキスト塊にまとめる（項目展開しない）。

    要約minute の全セクション＋amptalk ネイティブ提案機会検知＋会議議題を渡し、
    AI に「重複を排した distinct な 機会/リスク/タスク」を生成させる。
    顧客タスク（<顧客>）は素材に含めない（6/18決定）。
    """
    parts: list[str] = []
    if mtg.meeting_title:
        parts.append(f"【会議議題】{mtg.meeting_title}")
    if mtg.proposal.strip():
        parts.append(f"【amptalk提案機会検知（文字起こし由来）】\n{mtg.proposal.strip()}")

    for section_label, lines in [
        ("▼決定事項", mtg.decisions),
        ("▼タスク<ナイル>", mtg.tasks_nyle),
        ("▼BANT", mtg.bant),
        ("▼メモ", mtg.memo),
    ]:
        if not lines:
            continue
        parts.append(section_label)
        parts.extend(f"・{line}" for line in lines)

    return "\n".join(parts)


def _normalize_emoji(emoji: str) -> str:
    """AI の絵文字出力を 💰/🚨/● に正規化。不明・空・各種ビュレットは ● 扱い。"""
    e = (emoji or "").strip()
    if e in (EMOJI_UPSELL, "🆕"):   # 🆕 を返してきても 💰 に寄せる
        return EMOJI_UPSELL
    if e == EMOJI_RISK:
        return EMOJI_RISK
    return EMOJI_TASK


def _normalize_section(section: str) -> str:
    s = (section or "").strip()
    return s if s in VALID_SECTIONS else "decision"


def _normalize_category(category: str) -> str:
    """AI の領域出力を CATEGORY_ORDER の7枠に正規化。枠外・空は『その他』。"""
    c = (category or "").strip()
    return c if c in VALID_CATEGORIES else CATEGORY_FALLBACK


def _request_distinct(claude: ClaudeClient, blob: str, skill_content: str) -> list[dict]:
    """議事録1件の全内容(blob)を AI に渡し、重複を排した distinct な検知項目の
    JSON配列を得る。1議事録 = 1 API コール（過分割の根治）。
    """
    user_payload = {
        "task": (
            "以下は1つの商談（議事録）の全内容です。skill の判定ルール・採用基準・"
            "ラベル/サマリ生成ガイドに従い、この議事録から『重複を排した distinct な』"
            "機会(💰)・リスク(🚨)・タスク(●) のリストを生成してください。"
            "同じ機会/リスク/タスクが▼決定事項・▼タスク・▼メモ など複数箇所に繰り返し"
            "出てきても、必ず1件に統合し、逐語重複・言い換え重複を出力に残さないこと。"
            "結果は JSON 配列のみ（前後にテキストやコードブロックを付けない）。"
        ),
        "output_schema": [
            {
                "emoji": "string - 💰(アップセル機会) / 🚨(競争・離反リスク) / ●(普通のタスク・情報) のいずれか1つ",
                "label": "string - 8〜12字の体言止めテーマ見出し（【】記号なし）",
                "summary": "string - 自然な日本語のサマリ。情報量厚めに（80〜120字目安・必要なら2文）。具体の数字・固有名詞・背景・狙い（なぜ機会/リスクか）を盛り込む。期日表記は含めない",
                "category": "string - 領域分類。広告 / SEO / サイト改善 / コンテンツ / SNS運用 / 計測・データ基盤 / その他 のいずれか1つ",
                "source_section": "string - decision / task_nyle / bant / memo のいずれか（主な出どころ）",
                "due": "string - 期日があれば原文の期日表記（例 2026/07/03 や 今週中）。なければ空文字",
                "is_noise": "boolean - 採用しない項目の場合 true（出力配列から除外）",
            }
        ],
        "rules": [
            "★最重要: 同じ機会/リスク/タスクの重複は必ず1件に統合する（過分割を出さない）",
            "絵文字は3種類のみ・1項目1絵文字: 💰(アップセル機会＝提案でお金になりそう) / 🚨(競争・離反リスク＝取られる/失う) / ●(それ以外の拾うべきタスク・情報)",
            "💰 は『攻める価値あるアップセル機会』に付与（追加提案/新規領域/次フェーズ/内製化/予算拡大/新ソリューション 等）。金額小さい・高額難・単なる進行管理・事務TODOは ●",
            "🚨 は『競争リスク(コンペ/競合/再提案/体制見直し=取られる)』と『離反・成果リスク(流入減/順位下落/成果不振/解約示唆/強い不満=失う)』だけ。通常のデリバリー不満(WBS不足・接点頻度・進行透明性)は🚨にせず ●",
            "早急さ(至急/今週中等)は絵文字にせず due に期日として入れる(🏃マークは廃止)",
            "▼決定事項・▼タスク<ナイル>・▼BANT は情報があれば原則採用。『未確認』『特になし』だけは is_noise=true",
            "▼メモは『マネ＋AMが判断に使える情報』のみ採用。雑談・進捗・社内共有のみは is_noise=true",
            "category は7枠から必ず1つ付与: 広告(リスティング/SNS広告/運用型広告) / SEO(検索順位・オーガニック流入・内部対策・コンテンツSEO) / サイト改善(CRO・UI/UX・フォーム・LP・回遊改善) / コンテンツ(記事制作・リライト・編集・オウンドメディア) / SNS運用(Instagram/X等の運用・投稿・アカウント) / 計測・データ基盤(GA4・GTM・タグ・ダッシュボード・データ連携) / その他(上記に当てはまらない・複数横断)。施策/成果の内容で判断し、迷う場合は その他",
            "label は全項目に付与。8〜12字の体言止めで『何の機会/リスク/タスクか』を端的に。【】記号は付けない",
            "summary は『読んだだけで状況が分かる』情報量で書く（80〜120字目安・必要なら2文）。①何が起きたか（状況・経緯）②なぜ機会/リスク/タスクなのか（理由・背景）③だから何が必要か、が伝わること。例『コンペ再提案』だけでなく『代理店見直しでRFPが発行され、SEO/コンテンツを複数社コンペに。現契約のナイルも再提案を求められ、勝てないと失注リスク』のように背景と理由まで書く。抽象動詞(対応・検討・確認)を避け具体の数字・固有名詞を含める。無理な短縮・略語は禁止。期日表記は含めない（別途【期日】行で表示）",
            "結果は JSON 配列のみ。コードブロック・前置きなし",
        ],
        "minutes": blob,
    }

    user_input = json.dumps(user_payload, ensure_ascii=False)
    resp = claude.messages_create(
        system=skill_content,
        messages=[{"role": "user", "content": user_input}],
        max_tokens=4000,
    )

    text = "".join(
        block.get("text", "")
        for block in resp.get("content", [])
        if block.get("type") == "text"
    ).strip()

    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()

    try:
        results = json.loads(text)
    except json.JSONDecodeError:
        print(f"[distinct] JSON parse failed. Raw (head 500): {text[:500]}", flush=True)
        print(f"[distinct] JSON parse failed. Raw (tail 200): {text[-200:]}", flush=True)
        return []

    return results if isinstance(results, list) else []


# ========== Phase 6: 通知整形 ==========

# カテゴリ内の並び順（💰 機会 → 🚨 リスク → ● タスク）
_EMOJI_RANK = {EMOJI_UPSELL: 0, EMOJI_RISK: 1, EMOJI_TASK: 2}


def _format_item_block(it: DetectedItem) -> list[str]:
    """💰 機会・🚨 リスクの項目ブロック（2〜3行）。
    `絵文字 *ラベル*` ＋改行＋サマリ本文。期日があれば :date: 行を足す。
    絵文字は太字の外に出す（Slack mrkdwn は太字内の絵文字をショートコード化するため）。
    """
    label = (it.label or "").strip()
    head = f"{it.emoji} *{label}*" if label else it.emoji
    block = [head]
    if it.summary:
        block.append(it.summary)
    due = _format_due_for_notify(it)
    if due:
        block.append(f":date: {due}")
    return block


def _format_task_line(it: DetectedItem) -> str:
    """● タスク（期日付きのみ採用）の1行：`● ラベル　:date:期日`。"""
    label = (it.label or "").strip() or it.summary
    due = _format_due_for_notify(it)
    return f"{EMOJI_TASK} {label}　:date:{due}"


def build_parent_message(meeting: MinutesMeeting, items: list[DetectedItem]) -> str:
    """親メッセージ（v2.2・領域カテゴリ・グルーピング）：議事録1件分のヘッダ＋カテゴリ別。
    - CATEGORY_ORDER の固定順で表示。出番ゼロの枠は非表示
    - カテゴリ見出しは太字 `*【○○関連】*`
    - カテゴリ内は 💰→🚨→● 順。💰/🚨 は太字ラベル＋サマリ（＋期日行）、● は1行（期日付きのみ）
    """
    project_name = meeting.customer.strip() or extract_project_name(meeting.channel_name, meeting.call_name)
    clean_title = strip_prefix_from_call_name(meeting.call_name)

    lines = [
        "📌 下記MTGから提案機会を検知しました！",
        "",
        f"💼 *{project_name}*",
    ]
    # 会議タイトルが案件名と異なる場合のみ2行目に出す（重複表示防止）
    if clean_title and clean_title != project_name:
        lines.append(f"*{clean_title}*")
    lines.append(f"amptalk URL：{meeting.permalink}")
    # URL の下に区切り線
    lines.append("─" * 20)

    # 領域カテゴリで再分類（CATEGORY_ORDER 固定順、出番ゼロの枠は非表示）
    for category in CATEGORY_ORDER:
        cat_items = [i for i in items if _normalize_category(i.category) == category]
        if not cat_items:
            continue
        # カテゴリ内は 💰→🚨→● 順に安定ソート
        cat_items.sort(key=lambda i: _EMOJI_RANK.get(i.emoji, 99))

        lines.append("")
        lines.append(f"*【{category}関連】*")
        for it in cat_items:
            if it.emoji == EMOJI_TASK:
                lines.append(_format_task_line(it))
            else:
                lines.extend(_format_item_block(it))
                lines.append("")  # 機会/リスクブロックの後に空行
        # カテゴリ末尾の余分な空行を除去
        while lines and lines[-1] == "":
            lines.pop()

    return "\n".join(lines)


def _format_due_for_notify(item: DetectedItem) -> str:
    if item.due_date:
        try:
            dt = datetime.strptime(item.due_date, "%Y-%m-%d")
            wd = JP_WEEKDAYS[dt.weekday()]
            return f"{dt.strftime('%Y/%m/%d')}（{wd}）"
        except ValueError:
            return item.due_date
    if item.due_raw:
        return item.due_raw
    return ""


def slack_get_permalink(slack: SlackTools, channel_id: str, ts: str) -> str:
    if not ts:
        return ""
    try:
        resp = slack.client.chat_getPermalink(channel=channel_id, message_ts=ts)
        return resp.get("permalink", "")
    except Exception as e:
        print(f"[permalink error] {channel_id}/{ts}: {e}", flush=True)
        return ""


# ========== スプシ行構築 ==========

def build_sheet_row(item: DetectedItem) -> dict:
    # v2.2: F列「分類タグ」は source_section ではなく領域 category（絵文字＋領域）
    category = _normalize_category(item.category)
    if item.emoji:
        category_tag = f"{item.emoji} {category}"
    else:
        category_tag = category

    return {
        "議事録投稿日時": item.meeting_posted_at.strftime("%Y/%m/%d %H:%M"),
        "社外/社内": item.meeting_type,
        "チャンネル名": item.channel_name,
        "案件名": item.project_name,
        "会議タイトル": item.meeting_title,
        "分類タグ": category_tag,
        "サマリ": f"【{item.label}】{item.summary}" if item.label else item.summary,
        "期日（確定）": item.due_date,
        "期日（原文）": item.due_raw,
        "担当": "",
        "ステータス": "",
        "議事録URL": item.minutes_url,
        "通知URL": item.notification_url,
        "備考": "",
    }


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=JST).strftime("%Y/%m/%d %H:%M")
