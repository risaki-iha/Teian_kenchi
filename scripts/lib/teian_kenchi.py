"""
提案機会検知くん コア実装（2026-06-09 改訂版v2・議事録単位フォーマット）

設計方針:
- 議事録転送Bot (U0B305165M1) の投稿だけをスキャン
- 議事録を構造化パース → 各項目を AI 判定（絵文字🆕/🚨/🏃 or 空 + サマリ）
- **1議事録 = 1親メッセージ + 1スレッド子メッセージ**（▼メモあれば）
- **1議事録項目 = 1スプシ行**（▼メモ採用分も1行）
- セクション内の項目は絵文字優先順位順（🆕 > 🚨 > 🏃 > 空）に並べ替え
- ▼BANT セクションも対象（独立セクションとして表示）
- 「要約:」マーカー柔軟化（なくてもパース可能）、<議題>/<会議議題>両対応
- 初版: メンションなし通知（マネ＋AMメンションは別フェーズ）
- 初版: 期日リマインダーは別フェーズ
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

JST = timezone(timedelta(hours=9))
JP_WEEKDAYS = "月火水木金土日"

# 議事録転送Bot
MINUTES_BOT_USER_ID = "U0B305165M1"

# 通知先チャンネル
NOTIFICATION_CHANNEL = "C0AHUC1VDDK"  # #dxm_提案機会_検知くん

# 議事録パース用マーカー（柔軟化）
# 「要約:」があれば その以降を、なければ投稿全体を対象
EXTRACT_START_MARKERS = ["要約:", "要約：", "<会議議題>", "<議題>"]
EXTRACT_END_MARKERS = ["関係構築・心理的距離:", "関係構築・心理的距離："]

EVALUATE_BATCH_SIZE = 8

# 絵文字優先順位（複数該当時は左から、並び替えも左ほど上）
EMOJI_PRIORITY = ["🆕", "🚨", "🏃"]

# セクション見出し
SECTION_LABEL_FOR_SHEET = {
    "decision": "決定事項",
    "task_customer": "タスク<顧客>",
    "task_nyle": "タスク<ナイル>",
    "bant": "BANT",
    "memo": "メモ",
}

# 親メッセージは「絵文字軸」3セクション構成（マネ＋AM視点で攻める/守る/やる）
# 装飾なし項目とメモはスレッド子メッセージ「▼参考情報」へ
EMOJI_PARENT_SECTIONS = [
    ("🆕", "▼アップセル機会"),
    ("🚨", "▼リスク警告"),
    ("🏃", "▼タスク（TODO）"),
]

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


@dataclass
class DetectedItem:
    """検知された1項目（=スプシ1行）。Slack通知は議事録単位で集約される"""
    meeting_posted_at: datetime
    meeting_type: str            # 社外/社内
    channel_name: str
    channel_id: str
    project_name: str
    meeting_title: str
    emoji: str                   # 🆕 / 🚨 / 🏃 / "" のいずれか
    summary: str
    due_date: str                # YYYY-MM-DD or ""
    due_raw: str                 # 議事録上の原文
    minutes_url: str             # 議事録Bot投稿permalink
    source_section: str          # 'decision' | 'task_customer' | 'task_nyle' | 'bant' | 'memo'
    original_text: str
    notification_url: str = ""   # 親メッセージ投稿後に埋める
    meeting_key: str = ""        # 議事録単位の集約キー


# ========== メイン ==========

def run_teian_kenchi() -> None:
    """提案機会検知くん 本体"""
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

        # Phase 1: 検索範囲決定
        after_ts, before_ts = determine_search_range(slack)
        print(f"[range] {fmt_ts(after_ts)} 〜 {fmt_ts(before_ts)}", flush=True)

        # Phase 2: 議事録Bot投稿スキャン
        posts = scan_minutes_bot_posts(slack, after_ts, before_ts)
        print(f"[scan] {len(posts)} minutes posts", flush=True)

        # Phase 3: 議事録パース
        meetings: list[MinutesMeeting] = []
        for p in posts:
            m = parse_minutes_post(p)
            if m:
                meetings.append(m)
        print(f"[parse] {len(meetings)} meetings parsed", flush=True)

        if not meetings:
            _post_heartbeat(slack, after_ts, before_ts)
            print("[end] 議事録0件、終了", flush=True)
            return

        # Phase 4: 社外/社内振り分け & 項目展開
        items: list[DetectedItem] = []
        meeting_lookup: dict[str, MinutesMeeting] = {}
        meeting_context_map: dict[str, str] = {}

        for mtg in meetings:
            mtype = classify_meeting_type(mtg.call_name, mtg.meeting_title, mtg.tasks_customer, mtg.memo)
            # 社内MTGは完全スキップ（通知もスプシも対象外）
            # 提案機会検知くんは社外MTGからのアップセル機会検知が主目的
            if mtype == "社内":
                print(f"[skip] 社内MTG: {mtg.call_name}", flush=True)
                continue
            project = extract_project_name(mtg.channel_name, mtg.call_name)
            clean_title = strip_prefix_from_call_name(mtg.call_name)
            meeting_key = f"{mtg.channel_id}_{int(mtg.posted_at.timestamp())}"
            meeting_lookup[meeting_key] = mtg

            # AI判定用の議事録コンテキスト（会議タイトル＋▼決定事項冒頭で文脈ヒント）
            meeting_context_map[meeting_key] = _build_meeting_context(mtg)

            for section_name, lines in [
                ("decision", mtg.decisions),
                ("task_customer", mtg.tasks_customer),
                ("task_nyle", mtg.tasks_nyle),
                ("bant", mtg.bant),
                ("memo", mtg.memo),
            ]:
                for line in lines:
                    due_date, due_raw = extract_due_date(line)
                    items.append(DetectedItem(
                        meeting_posted_at=mtg.posted_at,
                        meeting_type=mtype,
                        channel_name=mtg.channel_name,
                        channel_id=mtg.channel_id,
                        project_name=project,
                        meeting_title=clean_title,
                        emoji="",
                        summary="",
                        due_date=due_date,
                        due_raw=due_raw,
                        minutes_url=mtg.permalink,
                        source_section=section_name,
                        original_text=line,
                        meeting_key=meeting_key,
                    ))
        print(f"[expand] {len(items)} items expanded", flush=True)

        # Phase 5: AI判定（絵文字・サマリ生成・ノイズ除外）
        items = evaluate_with_claude(claude, items, skill_content, meeting_context_map)
        print(f"[evaluate] {len(items)} items after AI filter", flush=True)

        if not items:
            _post_heartbeat(slack, after_ts, before_ts)
            print("[notify] 検知0件のため通知スキップ（ハートビートのみ投稿）", flush=True)
            return

        # Phase 6: 議事録ごとにグループ化 → 通知＆スプシ追記
        grouped: dict[str, list[DetectedItem]] = {}
        for it in items:
            grouped.setdefault(it.meeting_key, []).append(it)

        rows_to_append: list[dict] = []
        for meeting_key, group_items in grouped.items():
            mtg = meeting_lookup.get(meeting_key)
            if not mtg:
                continue

            # 親メッセージ投稿（セクション内を絵文字優先順位順に並べ替え済み）
            parent_text = build_parent_message(mtg, group_items)
            parent_resp = slack.post_message(NOTIFICATION_CHANNEL, parent_text)
            parent_ts = parent_resp.get("ts", "") if parent_resp.get("ok") else ""
            parent_permalink = (
                slack_get_permalink(slack, NOTIFICATION_CHANNEL, parent_ts)
                if parent_ts else ""
            )

            # スレッド子メッセージ投稿
            # 親メッセージから漏れた項目（装飾なし全部 + memoの装飾なし）を「▼参考情報」として展開
            thread_items = [i for i in group_items if i.emoji == ""]
            if thread_items and parent_ts:
                thread_text = build_thread_message(thread_items)
                _post_thread(slack, NOTIFICATION_CHANNEL, parent_ts, thread_text)

            # 全項目の notification_url に親メッセージpermalink を入れる
            for it in group_items:
                it.notification_url = parent_permalink
                rows_to_append.append(build_sheet_row(it))

            print(f"[notify] meeting={meeting_key} items={len(group_items)} posted", flush=True)

        if rows_to_append:
            appended = sheets.append_rows(rows_to_append)
            print(f"[sheets] appended {appended} rows", flush=True)
    finally:
        if claude.has_token_rotated():
            _emit_refresh_token_output(claude.get_current_refresh_token())


def _build_meeting_context(mtg: MinutesMeeting) -> str:
    """AI判定時に渡す議事録コンテキスト（会議タイトル＋▼決定事項冒頭1〜2行）"""
    lines = []
    if mtg.meeting_title:
        lines.append(f"会議議題: {mtg.meeting_title}")
    if mtg.decisions:
        decisions_excerpt = " / ".join(mtg.decisions[:2])
        lines.append(f"▼決定事項冒頭: {decisions_excerpt}")
    return " | ".join(lines)


def _post_heartbeat(slack: SlackTools, after_ts: int, before_ts: int) -> None:
    """検知0件でも稼働確認のため「💤 検知なし」をハートビート投稿する（将来オフ化予定）"""
    text = f"💤 検知なし\n対象範囲: {fmt_ts(after_ts)} 〜 {fmt_ts(before_ts)}"
    try:
        slack.post_message(NOTIFICATION_CHANNEL, text)
        print("[heartbeat] 検知0件のハートビート投稿", flush=True)
    except Exception as e:
        print(f"[heartbeat error] {e}", flush=True)


def _post_thread(slack: SlackTools, channel: str, thread_ts: str, text: str) -> None:
    try:
        slack.client.chat_postMessage(
            channel=channel,
            text=text,
            thread_ts=thread_ts,
            unfurl_links=False,
            unfurl_media=False,
        )
    except Exception as e:
        print(f"[thread post error] {e}", flush=True)


def _emit_refresh_token_output(token: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    print(f"::add-mask::{token}", flush=True)
    with open(output_path, "a", encoding="utf-8") as f:
        f.write(f"new_refresh_token={token}\n")
    print("[oauth] 🔁 GITHUB_OUTPUT に new_refresh_token を書き出した", flush=True)


# ========== Phase 1: 検索範囲 ==========

def determine_search_range(slack: SlackTools) -> tuple[int, int]:
    custom_after = (os.environ.get("CUSTOM_AFTER") or "").strip()
    custom_before = (os.environ.get("CUSTOM_BEFORE") or "").strip()
    if custom_after and custom_before:
        af = int(datetime.strptime(custom_after, "%Y-%m-%d %H:%M").replace(tzinfo=JST).timestamp())
        bf = int(datetime.strptime(custom_before, "%Y-%m-%d %H:%M").replace(tzinfo=JST).timestamp())
        return af, bf

    now_ts = int(datetime.now(JST).timestamp())
    messages = slack.read_channel_recent(NOTIFICATION_CHANNEL, limit=50)
    last_post_ts = None
    for msg in messages:
        text = msg.get("text", "")
        # 通知メッセージ側は「📌」絵文字あり、ただしSlack API レスポンスで
        # 絵文字が ":pushpin:" 表記になる場合があるため、検索は絵文字抜きで照合する
        if "下記MTGから提案機会を検知しました" in text:
            try:
                last_post_ts = int(float(msg.get("ts", "0")))
                break
            except (ValueError, TypeError):
                continue

    if last_post_ts:
        return last_post_ts, now_ts

    # フォールバック: 直近24時間（前回通知時刻が拾えなかった場合の取りこぼし防止）
    start = datetime.now(JST) - timedelta(hours=24)
    return int(start.timestamp()), now_ts


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


# ========== Phase 5: AI判定 ==========

def evaluate_with_claude(
    claude: ClaudeClient,
    items: list[DetectedItem],
    skill_content: str,
    meeting_context_map: dict[str, str],
) -> list[DetectedItem]:
    if not items:
        return []

    indexed = [(i, item) for i, item in enumerate(items)]
    all_results: dict[int, dict] = {}
    total_batches = (len(indexed) + EVALUATE_BATCH_SIZE - 1) // EVALUATE_BATCH_SIZE
    for batch_idx in range(0, len(indexed), EVALUATE_BATCH_SIZE):
        batch = indexed[batch_idx : batch_idx + EVALUATE_BATCH_SIZE]
        batch_no = batch_idx // EVALUATE_BATCH_SIZE + 1
        results = _evaluate_batch(claude, batch, skill_content, meeting_context_map)
        print(
            f"[evaluate] batch {batch_no}/{total_batches}: {len(batch)} items → {len(results)} results",
            flush=True,
        )
        for r in results:
            try:
                idx = int(r.get("item_id", -1))
                if 0 <= idx < len(items) and not r.get("is_noise", False):
                    all_results[idx] = r
            except (ValueError, TypeError):
                continue

    filtered = []
    for i, item in indexed:
        if i not in all_results:
            continue
        r = all_results[i]
        item.emoji = _normalize_emoji(r.get("emoji", ""))
        item.summary = r.get("summary", "") or ""
        if item.summary:
            filtered.append(item)
    return filtered


def _normalize_emoji(emoji: str) -> str:
    if emoji in EMOJI_PRIORITY:
        return emoji
    return ""


def _evaluate_batch(
    claude: ClaudeClient,
    batch: list[tuple[int, DetectedItem]],
    skill_content: str,
    meeting_context_map: dict[str, str],
) -> list[dict]:
    user_payload = {
        "task": "以下の議事録項目を skill の絵文字判定ルール・採用基準・サマリ生成ガイドに従って判定し、各項目に emoji（🆕/🚨/🏃/空文字のいずれか1つ）と summary（60字程度の自然な日本語サマリ、何の話か文脈含めて具体的に）を付与した JSON配列を返してください（前後にテキストを付けない）。",
        "output_schema": [
            {
                "item_id": "integer（入力の item_id をそのまま返す）",
                "emoji": "string - 🆕/🚨/🏃/空文字 のいずれか1つ",
                "summary": "string - 60字程度の自然な日本語サマリ",
                "is_noise": "boolean - 出力配列から除外したい場合 true",
            }
        ],
        "rules": [
            "絵文字判定: 🆕(アップセル機会・攻める価値ある予算系) > 🚨(競争リスク・顧客課題) > 🏃(早急) の優先順位で1項目1絵文字",
            "🆕 は『攻める価値ある時だけ』付与（予算情報あっても金額小さい・高額難なら装飾なし）",
            "シグナル語彙に該当しない通常項目は emoji='' (空文字)",
            "▼決定事項・▼タスク(顧客/ナイル)・▼BANT は『情報があれば原則すべて採用』。『未確認』『特になし』のみ除外（is_noise=true）",
            "▼メモは『マネ＋AMが判断に使える情報』のみ採用、雑談・進捗・社内共有のみは is_noise=true",
            "サマリは 60字程度（40〜70字許容）の自然な日本語。『何を』『誰が』『いつまでに』が分かる具体的な文章にする。会議タイトル・他項目の文脈（meeting_context）も参照して何の話か明示する",
            "抽象動詞（対応・検討・確認）は避け、具体的な目的語を含める",
            "結果は JSON 配列のみ。コードブロック・前置きなし",
        ],
        "items": [
            {
                "item_id": idx,
                "source_section": item.source_section,
                "meeting_type": item.meeting_type,
                "channel_name": item.channel_name,
                "meeting_title": item.meeting_title,
                "meeting_context": meeting_context_map.get(item.meeting_key, ""),
                "text": item.original_text,
            }
            for idx, item in batch
        ],
    }

    user_input = json.dumps(user_payload, ensure_ascii=False)
    resp = claude.messages_create(
        system=skill_content,
        messages=[{"role": "user", "content": user_input}],
        max_tokens=8000,
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
        print(f"[evaluate] JSON parse failed. Raw (head 500): {text[:500]}", flush=True)
        print(f"[evaluate] JSON parse failed. Raw (tail 200): {text[-200:]}", flush=True)
        return []

    return results if isinstance(results, list) else []


# ========== Phase 6: 通知整形 ==========

def _sort_by_emoji_priority(items: list[DetectedItem]) -> list[DetectedItem]:
    """セクション内の項目を絵文字優先順位順（🆕 > 🚨 > 🏃 > 空）で安定ソート"""
    def sort_key(it: DetectedItem) -> int:
        if it.emoji in EMOJI_PRIORITY:
            return EMOJI_PRIORITY.index(it.emoji)
        return len(EMOJI_PRIORITY)  # 空文字は最後
    return sorted(items, key=sort_key)


def build_parent_message(meeting: MinutesMeeting, items: list[DetectedItem]) -> str:
    """親メッセージ：議事録1件分のヘッダ＋絵文字軸3セクション。
    - 🆕 アップセル機会 / 🚨 リスク警告 / 🏃 タスク（TODO）
    - 元議事録のセクション（▼決定事項/▼タスク/▼BANT等）を横断して、絵文字で再分類
    - 装飾なし項目と ▼メモ採用分は親には載せず、スレッド子に回す
    """
    project_name = extract_project_name(meeting.channel_name, meeting.call_name)
    clean_title = strip_prefix_from_call_name(meeting.call_name)

    lines = [
        "📌 下記MTGから提案機会を検知しました！",
        "",
        f"💼 *{project_name}*",
    ]
    # 会議タイトルが案件名と異なる場合のみ2行目に出す（重複表示防止）
    if clean_title and clean_title != project_name:
        lines.append(f"*{clean_title}*")
    # 会議タイトル/案件名 と 議事録URL の間に1行空ける
    lines.append("")
    lines.append(f"議事録：{meeting.permalink}")
    # 議事録URL の下に区切り線
    lines.append("─" * 20)

    # 絵文字軸で再分類（元のセクション横断、絵文字付き項目だけ親メッセージへ）
    for emoji, label in EMOJI_PARENT_SECTIONS:
        section_items = [i for i in items if i.emoji == emoji]
        if not section_items:
            continue

        lines.append("")
        lines.append(f"*{label}*")
        for it in section_items:
            lines.append(f"{it.emoji} {it.summary}")
            lines.append(f"【期日】{_format_due_for_notify(it)}")
            lines.append("")
        if lines and lines[-1] == "":
            lines.pop()

    return "\n".join(lines)


def build_thread_message(thread_items: list[DetectedItem]) -> str:
    """スレッド子メッセージ：▼参考情報。
    - 装飾なし項目（emoji="") と ▼メモ採用分 を統合
    - 元議事録のセクションでグルーピングして表示（[決定事項] / [タスク<顧客>] / [タスク<ナイル>] / [BANT] / [メモ]）
    """
    if not thread_items:
        return ""

    # source_section でグルーピング
    grouped: dict[str, list[DetectedItem]] = {}
    for it in thread_items:
        section_label = SECTION_LABEL_FOR_SHEET.get(it.source_section, "")
        grouped.setdefault(section_label, []).append(it)

    # 表示順
    section_order = ["決定事項", "タスク<顧客>", "タスク<ナイル>", "BANT", "メモ"]

    lines = ["*▼参考情報*"]
    for section in section_order:
        items = grouped.get(section, [])
        if not items:
            continue
        lines.append("")
        lines.append(f"[{section}]")
        for it in items:
            head = f"{it.emoji} " if it.emoji else "・"
            lines.append(f"{head}{it.summary}")

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
    return "期日なし"


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
    section_label = SECTION_LABEL_FOR_SHEET.get(item.source_section, item.source_section)
    if item.emoji:
        category_tag = f"{item.emoji} {section_label}"
    else:
        category_tag = section_label

    return {
        "議事録投稿日時": item.meeting_posted_at.strftime("%Y/%m/%d %H:%M"),
        "社外/社内": item.meeting_type,
        "チャンネル名": item.channel_name,
        "案件名": item.project_name,
        "会議タイトル": item.meeting_title,
        "分類タグ": category_tag,
        "サマリ": item.summary,
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
