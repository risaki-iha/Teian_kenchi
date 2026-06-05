"""
提案機会検知くん専用 Google Sheets 書き込み（gspread + サービスアカウント）

設計方針:
- クレーム/解約検知 (`sheets_tools.py`) とは別スプシ・別シート・別列構成のため、
  既存 SheetsTools と並存する形で新規クラスとして提供する
- スプシID・シート名・列構成は本ファイル内に固定
- Bot は新規行追加（append）のみ、既存行は触らない（手動上書きを保護）
"""

import os
import json
import gspread
from google.oauth2.service_account import Credentials


SPREADSHEET_ID = "1UvbEP78vi10WttBcapWI9_Dpe9nvd7YHv2TLdiemIsA"
SHEET_NAME = "AI検知ログ"

# 列順（A〜N）— 2026-06-05 りさき調整版
COLUMNS = [
    "議事録投稿日時",   # A
    "社外/社内",        # B
    "チャンネル名",     # C
    "案件名",           # D
    "会議タイトル",     # E
    "分類タグ",         # F
    "サマリ",           # G
    "期日（確定）",     # H
    "期日（原文）",     # I
    "担当",             # J
    "ステータス",       # K
    "議事録URL",        # L
    "通知URL",          # M
    "備考",             # N
]


class TeianSheetsTools:
    def __init__(self):
        sa_json = os.environ.get("GOOGLE_SHEETS_KEY")
        if not sa_json:
            raise RuntimeError("GOOGLE_SHEETS_KEY 環境変数が必要（サービスアカウント JSON）")

        # 先頭に BOM (﻿) が混入している場合があるので除去
        creds_dict = json.loads(sa_json.lstrip("﻿"))
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        self.gc = gspread.authorize(creds)
        self.sheet = self.gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)

    def append_rows(self, rows: list[dict]) -> int:
        """rows: dict のリスト（key は COLUMNS）"""
        values = [[row.get(col, "") for col in COLUMNS] for row in rows]
        self.sheet.append_rows(values, value_input_option="USER_ENTERED")
        return len(values)
