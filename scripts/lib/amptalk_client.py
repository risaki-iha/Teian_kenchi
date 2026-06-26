"""amptalk API クライアント（提案機会検知くん v2 文字起こし駆動版）

- コール一覧（/v1/calls）を callDatetime の期間で取得
- コール分析（/v1/calls/{id}/analysis）から「文字起こし（話者ラベル付き）」
  「要約minute（▼決定事項等の構造化要約＝Slack議事録Bot投稿と同形式）」
  「提案機会検知minute（amptalkネイティブ）」「発話割合」を取得
- amptalk コール詳細URL から call_id(UUID) を抽出

認証: GitHub Actions では AMPTALK_API_KEY / AMPTALK_AUTH_KEY を Secrets から渡す。
      ローカルテスト時は配布キットの .env を load_dotenv() で読める（任意）。
"""

import os
import re
import time
from datetime import datetime, timezone, timedelta

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

JST = timezone(timedelta(hours=9))


def base_url() -> str:
    return os.environ.get("AMPTALK_BASE_URL", "https://api.amptalk.net/v1").rstrip("/")


# amptalk コール詳細URL（https://amptalk.net/calls/detail?id=<UUID>）から id を抜く
_CALL_ID_FROM_URL = re.compile(r"[?&]id=([0-9a-fA-F-]{36})")
_UUID = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

# /analysis の minutes[].title
MINUTE_SUMMARY = "要約"          # ▼決定事項/▼タスク/▼BANT/▼メモ の構造化要約
MINUTE_PROPOSAL = "提案機会検知"  # amptalk ネイティブの提案機会検知（【ニーズ①】…）
MINUTE_RATIO = "発話割合"         # スピーカーF（40%）…


def _headers() -> dict:
    return {
        "X-Api-Key": os.environ.get("AMPTALK_API_KEY", ""),
        "X-Authorization-Key": os.environ.get("AMPTALK_AUTH_KEY", ""),
        "Accept": "application/json",
    }


def _get(url: str, params: dict | None = None):
    """GET。200→json / 404→None / 429・5xx→指数バックオフ3回 / それ以外→raise。"""
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_headers(), params=params, timeout=30, verify=False)
        except requests.exceptions.RequestException:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return None
        if r.status_code in (429, 500, 502, 503, 504):
            if attempt == 2:
                r.raise_for_status()
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
    return None


def extract_call_id(url_or_text: str) -> str:
    """amptalk URL / テキストから call_id(UUID, 小文字) を抽出。なければ ''。"""
    if not url_or_text:
        return ""
    m = _CALL_ID_FROM_URL.search(url_or_text)
    if m:
        return m.group(1).lower()
    m = _UUID.search(url_or_text)
    return m.group(1).lower() if m else ""


def call_detail_url(call_id: str) -> str:
    return f"https://amptalk.net/calls/detail?id={call_id}"


def is_external_title(title: str) -> bool:
    """コール名(title)から社外判定。

    - 【社内】/社内_/【内部】 を含めば社外ではない（False）
    - 【社外】/社外_/【外部】/【確定】/【Zoom】 を含めば社外（True）
    - どちらのプレフィクスも無ければ判定不能 → False（保守的にスキップ）
    """
    t = str(title or "")
    if any(x in t for x in ["【社内】", "社内_", "【内部】"]):
        return False
    return any(x in t for x in ["【社外】", "社外_", "【外部】", "【確定】", "【Zoom】"])


def fetch_calls(start_dt: datetime, end_dt: datetime, page_size: int = 300) -> list[dict]:
    """callDatetime が [start_dt, end_dt] のコールをページネーションで全取得。

    返る各要素: {id, customer, callDatetime, hostId, title, durationSeconds}
    """
    def iso(dt: datetime) -> str:
        return dt.astimezone(JST).isoformat()

    url = f"{base_url()}/calls"
    items: list[dict] = []
    token = None
    while True:
        params = {"from": iso(start_dt), "to": iso(end_dt), "pageSize": page_size}
        if token:
            params["pageToken"] = token
        data = _get(url, params)
        if not isinstance(data, dict):
            break
        items.extend(data.get("data", []) or [])
        token = data.get("nextPageToken")
        if not token:
            break
    return items


def fetch_analysis(call_id: str) -> dict | None:
    """call の analysis を取得して必要部分を整形して返す。未取得/404 は None。"""
    data = _get(f"{base_url()}/calls/{call_id}/analysis")
    if not isinstance(data, dict):
        return None

    # 文字起こし（話者ラベル付き）
    lines: list[str] = []
    for t in data.get("transcriptions", []) or []:
        speaker = t.get("speakerName", "不明")
        text = (t.get("text") or "").strip()
        if text:
            lines.append(f"{speaker}: {text}")
    transcript = "\n".join(lines)

    minutes = {
        m.get("title"): (m.get("value") or "")
        for m in (data.get("minutes", []) or [])
    }

    return {
        "call_id": call_id,
        "is_finished": bool(data.get("isAnalysisFinished", True)),
        "transcript": transcript,
        "summary": minutes.get(MINUTE_SUMMARY, ""),     # Slack投稿本文と同形式
        "proposal": minutes.get(MINUTE_PROPOSAL, ""),   # amptalkネイティブ提案機会検知
        "speaking_ratio": minutes.get(MINUTE_RATIO, ""),
        "char_count": len(transcript),
    }
