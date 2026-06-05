"""
Claude OAuth クライアント

Claude Code サブスクの OAuth トークンを使って Anthropic API を呼ぶ。
これにより Anthropic API の従量課金が発生しない（サブスク内で完結）。

amptalk-risk-detection と同じ仕組み。

仕様:
- 環境変数 CLAUDE_REFRESH_TOKEN からリフレッシュトークンを読む
- リフレッシュエンドポイントで access_token を取得
- Authorization: Bearer <access_token> で Messages API を叩く
- access_token は一定時間有効（expires_in 秒）

自動ローテーション:
- OAuth refresh で新 refresh_token を受け取った場合、self.refresh_token を更新する
- 呼び出し側（detector.py）が get_current_refresh_token() で最新値を取り出し、
  GITHUB_OUTPUT 経由で workflow の後続 Step に渡して Secret 書き戻しさせる

フォールバック:
- ANTHROPIC_API_KEY が設定されてればそちらを使う（従量課金）
"""

import os
import time
import requests

OAUTH_TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
MESSAGES_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"


class ClaudeAuthError(Exception):
    pass


class ClaudeClient:
    """
    OAuth (refresh token) もしくは API キーで Anthropic API を叩く統一クライアント。
    amptalk方式：refresh_token があれば OAuth、無ければ API key にフォールバック。
    """

    def __init__(self):
        self.refresh_token = (os.environ.get("CLAUDE_REFRESH_TOKEN") or "").lstrip("﻿").strip() or None
        self._initial_refresh_token = self.refresh_token
        self.api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").lstrip("﻿").strip() or None
        self._access_token = None
        self._access_token_expires_at = 0

        if not self.refresh_token and not self.api_key:
            raise ClaudeAuthError(
                "CLAUDE_REFRESH_TOKEN または ANTHROPIC_API_KEY のどちらかが必要"
            )

    def has_token_rotated(self) -> bool:
        """OAuth refresh が成功して新トークンを受け取った場合のみ True。"""
        return (
            self.refresh_token is not None
            and self.refresh_token != self._initial_refresh_token
        )

    def _refresh_access_token(self):
        """OAuth トークンをリフレッシュ。Anthropic OAuth エンドポイントは form-encoded を要求。"""
        resp = requests.post(
            OAUTH_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": OAUTH_CLIENT_ID,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if resp.status_code != 200:
            # JSON フォールバック（古い実装互換）
            resp = requests.post(
                OAUTH_TOKEN_URL,
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                    "client_id": OAUTH_CLIENT_ID,
                },
                timeout=30,
            )
        if resp.status_code != 200:
            raise ClaudeAuthError(
                f"OAuth refresh failed: {resp.status_code} {resp.text}"
            )
        data = resp.json()
        self._access_token = data["access_token"]
        self._access_token_expires_at = time.time() + data.get("expires_in", 3600) - 60

        # refresh_token がローテーションされる場合は新しい値を採用
        # 呼び出し側が get_current_refresh_token() で取り出して GITHUB_OUTPUT に書く想定
        new_refresh = data.get("refresh_token")
        if new_refresh:
            self.refresh_token = new_refresh

    def _ensure_access_token(self):
        if self.refresh_token and (
            not self._access_token or time.time() >= self._access_token_expires_at
        ):
            self._refresh_access_token()

    def _build_headers(self):
        if self.refresh_token:
            self._ensure_access_token()
            return {
                "Authorization": f"Bearer {self._access_token}",
                "anthropic-beta": "oauth-2025-04-20",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def messages_create(
        self,
        *,
        system: str,
        messages: list,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 8000,
        tools: list | None = None,
    ) -> dict:
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            body["tools"] = tools

        resp = self._post_with_retry(body)
        if resp.status_code == 401 and self.refresh_token:
            # アクセストークン期限切れの可能性 → 強制リフレッシュして再試行
            self._access_token = None
            resp = self._post_with_retry(body)
        if resp.status_code != 200:
            raise ClaudeAuthError(
                f"Messages API failed: {resp.status_code} {resp.text[:500]}"
            )
        return resp.json()

    def _post_with_retry(self, body: dict, max_retries: int = 4):
        """429 / 5xx に対して指数バックオフでリトライ"""
        delay = 5.0
        for attempt in range(max_retries):
            resp = requests.post(
                MESSAGES_URL, headers=self._build_headers(), json=body, timeout=180
            )
            if resp.status_code < 500 and resp.status_code != 429:
                return resp
            # 429 or 5xx → リトライ
            retry_after = resp.headers.get("retry-after")
            sleep_for = float(retry_after) if retry_after else delay
            print(
                f"[claude retry] status={resp.status_code} attempt={attempt+1}/{max_retries} sleep={sleep_for}s",
                flush=True,
            )
            time.sleep(sleep_for)
            delay *= 2
        return resp

    def get_current_refresh_token(self) -> str | None:
        """ローテーションされた最新の refresh_token を返す（GitHub Secrets 更新用）"""
        return self.refresh_token
