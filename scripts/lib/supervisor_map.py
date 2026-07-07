"""
上長メンション解決（マスタスプシから AM 上長を引く）

設計方針:
- マスタスプシ「【公開用】案件ごとAM一覧」を読み、channel_id / コール名 → (マネ名, マネメアド) の辞書を作る
- 検知チャンネルの channel_id (G列) 完全マッチを優先、ダメならコール名 (B列) でチャンネル名部分マッチ
- 解決したマネのメアド (E列) を Slack の users.list 由来の email → user_id 辞書から user_id に変換してメンション化
- メアドで引けなければマネ名 (D列) → user_id 辞書をフォールバック
- 一覧で引けない / user_id を解決できない全ケースは DEFAULT_MENTION_EMAIL（宮澤）にフォールバック
- 宮澤すら user_id を引けなければ None（呼び出し側で【マネージャー】行を出さない＝事故メンション防止）
- テスト時はスプシのD列/E列を直接書き換えて運用する（環境変数による差し替えは行わない）
"""

import json
import os

import gspread
from google.oauth2.service_account import Credentials

MASTER_SPREADSHEET_ID = "1PWqW08yD6shJu5sRUxTZvf7w7K7TaJEuQcDU2QmkZXY"
MASTER_SHEET_NAME = "1_顧客一覧"

# 一覧にいない案件、または一覧にはいるが user_id を解決できなかった案件の
# 上長メンション フォールバック先（宮澤）。
DEFAULT_MENTION_EMAIL = "toru_my@nyle.co.jp"

# データ行は4行目(1-indexed)から。1〜3行目は注釈/見出し。
DATA_START_ROW_INDEX = 3
COL_CALL_NAME = 1     # B列
COL_MANAGER = 3       # D列（slackメンション先 ※マネ）
COL_EMAIL = 4         # E列（MGメアド）
COL_SLACK_CH_ID = 6   # G列（slackチャンネルID）


class SupervisorResolver:
    def __init__(self):
        self._by_channel_id: dict[str, dict[str, str]] = {}
        self._by_call_name: dict[str, dict[str, str]] = {}
        self._loaded = False

    def load(self, gc: gspread.Client | None = None) -> None:
        if gc is None:
            gc = _build_gspread_client()
        ws = gc.open_by_key(MASTER_SPREADSHEET_ID).worksheet(MASTER_SHEET_NAME)
        rows = ws.get_all_values()
        self.load_from_rows(rows)

    def load_from_rows(self, rows: list[list[str]]) -> None:
        """テスト用：rows を直接受け取って辞書を構築する。"""
        for i, row in enumerate(rows):
            if i < DATA_START_ROW_INDEX:
                continue
            # 短い行はスキップ（必要な列まで届かないため）
            if len(row) <= COL_SLACK_CH_ID:
                continue
            call_name = (row[COL_CALL_NAME] or "").strip()
            manager = (row[COL_MANAGER] or "").strip()
            email = (row[COL_EMAIL] or "").strip()
            ch_id = (row[COL_SLACK_CH_ID] or "").strip()
            if not manager and not email:
                continue
            entry = {"name": manager, "email": email}
            if ch_id:
                self._by_channel_id[ch_id] = entry
            if call_name:
                # コール名重複時は先勝ち（手前のほうが主案件と仮定）
                self._by_call_name.setdefault(call_name, entry)
        self._loaded = True

    def resolve_entry(self, channel_id: str, channel_name: str) -> dict | None:
        if not self._loaded:
            return None
        if channel_id:
            entry = self._by_channel_id.get(channel_id)
            if entry:
                return entry
        if channel_name:
            for call_name, entry in self._by_call_name.items():
                if call_name in channel_name:
                    return entry
        return None

    def resolve_mention(
        self,
        channel_id: str,
        channel_name: str,
        user_id_by_name: dict,
        user_id_by_email: dict,
        default_email: str | None = None,
    ) -> str | None:
        """
        検知チャンネル → Slack メンション文字列 (<@U0XXX>) を返す。
        メアド(E列)からの解決を優先、名前(D列)はフォールバック。
        一覧で引けない / user_id を解決できない場合は default_email（宮澤）で再解決する。
        それでも引けなければ None。
        """
        entry = self.resolve_entry(channel_id, channel_name)
        if entry:
            email = entry.get("email", "")
            if email:
                uid = user_id_by_email.get(email)
                if uid:
                    return f"<@{uid}>"
            name = entry.get("name", "")
            if name:
                uid = user_id_by_name.get(name)
                if uid:
                    return f"<@{uid}>"
        # 一覧外 or user_id 解決不能 → 宮澤にフォールバック
        if default_email:
            uid = user_id_by_email.get(default_email)
            if uid:
                return f"<@{uid}>"
        return None


def _build_gspread_client() -> gspread.Client:
    sa_json = os.environ.get("GOOGLE_SHEETS_KEY")
    if not sa_json:
        raise RuntimeError("GOOGLE_SHEETS_KEY 環境変数が必要（サービスアカウント JSON）")
    creds_dict = json.loads(sa_json.lstrip("﻿"))
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    return gspread.authorize(creds)
