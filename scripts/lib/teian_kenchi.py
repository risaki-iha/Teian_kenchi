"""
提案機会検知くん コア実装（2026-06-09 改訂版・議事録単位フォーマット）

設計方針:
- 議事録転送Bot (U0B305165M1) の投稿だけをスキャン
- 議事録を構造化パース → 各項目を AI 判定（絵文字🆕/🚨/🏃 or 空 + サマリ）
- **1議事録 = 1親メッセージ + 1スレッド子メッセージ**（▼メモあれば）
- **1議事録項目 = 1スプシ行**（▼メモ採用分も1行）
- 初版: メンションなし通知（マネ＋AMメンションは別フェーズ）
- 初版: 期日リマインダーは別フェーズ
"""

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

# 議事録パース用マーカー
EXTRACT_START_MARKER = "要約:"
EXTRACT_END_MARKER = "関係構築・心理的距離:"

EVALUATE_BATCH_SIZE = 8

# 絵文字優先順位（複数該当時は左から選ぶ）
EMOJI_PRIORITY = ["🆕", "🚨", "🏃"]

# セクション見出し
SECTION_LABEL_FOR_NOTIFY = {
    "decision": "▼決定事項",
    "task_customer": "▼タスク<顧客>",
    "task_nyle": "▼タスク<ナイル>",
    "memo": "▼その他留意事項",
}

SECTION_LABEL_FOR_SHEET = {
    "decision": "決定事項",
    "task_customer": "タスク<顧客>",
    "task_nyle": "タスク<ナイル>",
    "memo": "メモ",
}


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
    source_section: str          # 'decision' | 'task_customer' | 'task_nyle' | 'memo'
    original_text: str
    notification_url: str = ""   # 親メッセージ投稿後に埋める
    meeting_key: str = ""        # 議事録単位の集約キー（channel_id + posted_at）


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
            print("[end] 議事録0件、終了", flush=True)
            return

        # Phase 4: 社外/社内振り分け & 項目展開
        items: list[DetectedItem] = []
        meeting_lookup: dict[str, MinutesMeeting] = {}
        for mtg in meetings:
            mtype = classify_meeting_type(mtg.call_name, mtg.meeting_title, mtg.tasks_customer, mtg.memo)
            project = extract_project_name(mtg.channel_name, mtg.call_name)
            clean_title = strip_prefix_from_call_name(mtg.call_name)
            meeting_key = f"{mtg.channel_id}_{int(mtg.posted_at.timestamp())}"
            meeting_lookup[meeting_key] = mtg

            for section_name, lines in [
                ("decision", mtg.decisions),
                ("task_customer", mtg.tasks_customer),
                ("task_nyle", mtg.tasks_nyle),
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
        items = evaluate_with_claude(claude, items, skill_content)
        print(f"[evaluate] {len(items)} items after AI filter", flush=True)

        if not items:
            print("[notify] 検知0件のため通知スキップ", flush=True)
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

            # 親メッセージ投稿
            parent_text = build_parent_message(mtg, group_items)
            parent_resp = slack.post_message(NOTIFICATION_CHANNEL, parent_text)
            parent_ts = parent_resp.get("ts", "") if parent_resp.get("ok") else ""
            parent_permalink = (
                slack_get_permalink(slack, NOTIFICATION_CHANNEL, parent_ts)
                if parent_ts else ""
            )

            # スレッド子メッセージ投稿（▼メモ採用分があれば）
            memo_items = [i for i in group_items if i.source_section == "memo"]
            if memo_items and parent_ts:
                thread_text = build_thread_message(memo_items)
                _post_thread(slack, NOTIFICATION_CHANNEL, parent_ts, thread_text)

            # 全項目の notification_url に親メッセージpermalink を入れる
            for it in group_items:
                it.notification_url = parent_permalink
                rows_to_append.append(build_sheet_row(it))

            print(f"[notify] meeting={meeting_key} items={len(group_items)} posted", flush=True)

        # スプシに一括書き込み
        if rows_to_append:
            appended = sheets.append_rows(rows_to_append)
            print(f"[sheets] appended {appended} rows", flush=True)
    finally:
        if claude.has_token_rotated():
            _emit_refresh_token_output(claude.get_current_refresh_token())


def _post_thread(slack: SlackTools, channel: str, thread_ts: str, text: str) -> None:
    """親メッセージのスレッドに子メッセージを投稿"""
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
    """前回通知ベース。なければフォールバック（直近2時間）。"""
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
        # 提案機会検知くんの新フォーマットマーカー
        if "📌 下記MTGから提案機会を検知しました" in text:
            try:
                last_post_ts = int(float(msg.get("ts", "0")))
                break
            except (ValueError, TypeError):
                continue

    if last_post_ts:
        return last_post_ts, now_ts

    # フォールバック: 直近2時間
    start = datetime.now(JST) - timedelta(hours=2)
    return int(start.timestamp()), now_ts


# ========== Phase 2: スキャン ==========

def scan_minutes_bot_posts(slack: SlackTools, after_ts: int, before_ts: int) -> list[dict]:
    return _search_bot_messages(slack, MINUTES_BOT_USER_ID, after_ts, before_ts, limit=50)


def _search_bot_messages(slack: SlackTools, bot_user_id: str, after_ts: int, before_ts: int, limit: int = 50) -> list[dict]:
    """Bot投稿を含む検索。slack_tools.search の -is:bot を回避するため独自実装。"""
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
    text = post.get("text", "")
    body = extract_target_range(text)
    if not body:
        return None

    call_name = extract_call_name(text)
    meeting_title = extract_meeting_title(body)

    decisions = extract_section_lines(body, "▼決定事項")
    tasks_block = extract_section_block(body, "▼タスク")
    tasks_customer = extract_subsection_lines(tasks_block, "<顧客>")
    tasks_nyle = extract_subsection_lines(tasks_block, "<ナイル>")
    memo = extract_section_lines(body, "▼メモ")

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
        memo=memo,
    )


def extract_target_range(text: str) -> str:
    start = text.find(EXTRACT_START_MARKER)
    if start == -1:
        return ""
    end = text.find(EXTRACT_END_MARKER, start)
    if end == -1:
        return text[start:]
    return text[start:end].strip()


def extract_call_name(text: str) -> str:
    m = re.search(r"コール名[:：]\s*(.+)", text)
    if m:
        return m.group(1).strip()
    first_line = text.strip().split("\n", 1)[0]
    return first_line.strip()


def extract_meeting_title(body: str) -> str:
    m = re.search(r"<会議議題>\s*\n(.+?)(?=\n▼|\n<|\Z)", body, re.DOTALL)
    return m.group(1).strip() if m else ""


def extract_section_lines(body: str, section_marker: str) -> list[str]:
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
    lines = []
    for raw in text.split("\n"):
        stripped = raw.strip().lstrip("　 ")
        if stripped.startswith("・"):
            content = stripped.lstrip("・").strip()
            if content and content not in ["特になし", "特段なし", "なし", "ー", "-"]:
                lines.append(content)
    return lines


# ========== Phase 4: 社外/社内振り分け & 案件名抽出 ==========

def classify_meeting_type(call_name: str, title: str, tasks_customer: list[str], memo: list[str]) -> str:
    if any(x in call_name for x in ["【社外】", "社外_", "【外部】"]):
        return "社外"
    if any(x in call_name for x in ["【社内】", "社内_", "【内部】"]):
        return "社内"
    if "【確定】" in call_name:
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
    for prefix in ["【社外】", "【社内】", "【確定】", "【外部】", "【内部】", "社外_", "社内_"]:
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

def evaluate_with_claude(claude: ClaudeClient, items: list[DetectedItem], skill_content: str) -> list[DetectedItem]:
    """各項目に絵文字・サマリをAI判定。ノイズはフィルタする"""
    if not items:
        return []

    indexed = [(i, item) for i, item in enumerate(items)]
    all_results: dict[int, dict] = {}
    total_batches = (len(indexed) + EVALUATE_BATCH_SIZE - 1) // EVALUATE_BATCH_SIZE
    for batch_idx in range(0, len(indexed), EVALUATE_BATCH_SIZE):
        batch = indexed[batch_idx : batch_idx + EVALUATE_BATCH_SIZE]
        batch_no = batch_idx // EVALUATE_BATCH_SIZE + 1
        results = _evaluate_batch(claude, batch, skill_content)
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
        # サマリが空のものはスキップ（AI判定不全）
        if item.summary:
            filtered.append(item)
    return filtered


def _normalize_emoji(emoji: str) -> str:
    """AI出力の絵文字を正規化（不正値は空文字に）"""
    if emoji in EMOJI_PRIORITY:
        return emoji
    return ""


def _evaluate_batch(claude: ClaudeClient, batch: list[tuple[int, DetectedItem]], skill_content: str) -> list[dict]:
    """1バッチをClaude APIに投げる"""
    user_payload = {
        "task": "以下の議事録項目を skill の絵文字判定ルールに従って判定し、各項目に emoji（🆕/🚨/🏃/空文字のいずれか1つ）と summary（40字程度1行）を付与した JSON配列を返してください（前後にテキストを付けない）。",
        "output_schema": [
            {
                "item_id": "integer（入力の item_id をそのまま返す）",
                "emoji": "string - 🆕/🚨/🏃/空文字 のいずれか1つ",
                "summary": "string - 40字程度の1行サマリ",
                "is_noise": "boolean - 出力配列から除外したい場合 true",
            }
        ],
        "rules": [
            "絵文字判定: 🆕(アップセル機会+予算系) > 🚨(競争・リスク+顧客課題) > 🏃(早急) の優先順位で1項目1絵文字",
            "シグナル語彙に該当しない通常項目は emoji='' (空文字)",
            "▼決定事項 (source_section='decision') / ▼タスク (source_section='task_customer' or 'task_nyle') は原則すべて出力（特になし等のみ is_noise=true）",
            "▼メモ (source_section='memo') は『マネ＋AMが判断に使える情報』のみ出力。雑談・進捗確認・社内共有のみは is_noise=true",
            "結果は JSON 配列のみ。コードブロック・前置きなし",
        ],
        "items": [
            {
                "item_id": idx,
                "source_section": item.source_section,
                "meeting_type": item.meeting_type,
                "channel_name": item.channel_name,
                "meeting_title": item.meeting_title,
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

def build_parent_message(meeting: MinutesMeeting, items: list[DetectedItem]) -> str:
    """親メッセージ：議事録1件分のヘッダ＋▼決定事項/▼タスク<顧客>/▼タスク<ナイル>セクション"""
    project_name = extract_project_name(meeting.channel_name, meeting.call_name)
    clean_title = strip_prefix_from_call_name(meeting.call_name)
    title_line = f"{project_name} ／ {clean_title}" if clean_title else project_name

    lines = [
        "📌 下記MTGから提案機会を検知しました！",
        "",
        f"*{title_line}*",
        f"議事録：{meeting.permalink}",
    ]

    # ▼メモ以外のセクションを順番に並べる
    sections_order = ["decision", "task_customer", "task_nyle"]
    for section in sections_order:
        section_items = [i for i in items if i.source_section == section]
        if not section_items:
            continue
        lines.append("")
        lines.append(f"*{SECTION_LABEL_FOR_NOTIFY[section]}*")
        for it in section_items:
            head = f"{it.emoji} " if it.emoji else " "
            lines.append(f"{head}{it.summary}")
            lines.append(f"【期日】{_format_due_for_notify(it)}")
            lines.append("")
        # 末尾の空行を1つ詰める
        if lines and lines[-1] == "":
            lines.pop()

    return "\n".join(lines)


def build_thread_message(memo_items: list[DetectedItem]) -> str:
    """スレッド子メッセージ：▼その他留意事項（=▼メモから採用された項目）"""
    lines = ["*▼その他留意事項*"]
    for it in memo_items:
        head = f"{it.emoji} " if it.emoji else " "
        lines.append(f"{head}{it.summary}")
    return "\n".join(lines)


def _format_due_for_notify(item: DetectedItem) -> str:
    """通知用の期日表記"""
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
    """スプシ14列の dict を構築（F列「分類タグ」= 絵文字＋セクション名）"""
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
        "担当": "",  # 初版空欄、PR2 で supervisor_map 流用予定
        "ステータス": "",
        "議事録URL": item.minutes_url,
        "通知URL": item.notification_url,
        "備考": "",
    }


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=JST).strftime("%Y/%m/%d %H:%M")
