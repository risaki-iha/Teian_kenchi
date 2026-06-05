"""
Slack 操作ラッパー（Bot Token 経由）
"""

import os
import time
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler


class SlackTools:
    def __init__(self):
        bot_token = os.environ.get("SLACK_BOT_TOKEN", "").lstrip("﻿").strip()
        if not bot_token:
            raise RuntimeError("SLACK_BOT_TOKEN 環境変数が必要")
        # search.messages は User Token (xoxp-) でしか動かないため別クライアント
        user_token = os.environ.get("SLACK_USER_TOKEN", "").lstrip("﻿").strip()

        self.client = WebClient(token=bot_token)
        self.search_client = WebClient(token=user_token) if user_token else self.client
        # search.messages はレート制限が厳しいので自動リトライを有効化
        retry_handler = RateLimitErrorRetryHandler(max_retry_count=3)
        self.search_client.retry_handlers.append(retry_handler)
        self.client.retry_handlers.append(retry_handler)

    def search(
        self,
        query: str,
        after_ts: int,
        before_ts: int,
        limit: int = 20,
    ) -> list[dict]:
        """
        Slack の search.messages を叩く。User Token 必須。
        after/before は Unix秒（JST解釈は呼び出し元）
        """
        # Slackのsearch構文 after:/before: は exclusive（その日「より後/前」）。
        # 同日範囲だと何もヒットしないため、afterは1日前/beforeは1日後でクエリし、
        # 実際の絞り込みは戻り値の ts で行う。
        full_query = (
            f"{query} after:{_ymd(after_ts, -1)} before:{_ymd(before_ts, 1)} -is:bot"
        )
        try:
            resp = self.search_client.search_messages(query=full_query, count=limit, sort="timestamp")
        except SlackApiError as e:
            print(f"[search error] {query}: {e.response['error']}", flush=True)
            return []
        # 念のため呼び出し間に小休止（連続呼び出しで rate limit に当たる事故予防）
        time.sleep(1.0)

        matches = resp.get("messages", {}).get("matches", []) or []
        # ts 範囲で再フィルタ（API の after/before は荒いため）
        out = []
        for m in matches:
            ts = float(m.get("ts", 0))
            if after_ts <= ts < before_ts:
                out.append(m)
        return out

    def read_thread(self, channel: str, thread_ts: str) -> list[dict]:
        # User Token を優先（Botがチャンネル未参加でも、Userが参加してれば読める）
        try:
            resp = self.search_client.conversations_replies(
                channel=channel, ts=thread_ts, limit=200
            )
            return resp.get("messages", []) or []
        except SlackApiError as e:
            # User Token で読めなければ Bot Token にフォールバック
            try:
                resp = self.client.conversations_replies(
                    channel=channel, ts=thread_ts, limit=200
                )
                return resp.get("messages", []) or []
            except SlackApiError as e2:
                print(
                    f"[read_thread error] {channel}/{thread_ts}: user={e.response['error']} bot={e2.response['error']}",
                    flush=True,
                )
                return []

    def read_user_profile(self, user_id: str) -> dict:
        try:
            resp = self.client.users_info(user=user_id)
            user = resp.get("user", {}) or {}
            profile = user.get("profile", {}) or {}
            return {
                "id": user_id,
                "name": profile.get("display_name") or profile.get("real_name") or user.get("name", ""),
                "email": profile.get("email", ""),
            }
        except SlackApiError as e:
            print(f"[read_user_profile error] {user_id}: {e.response['error']}", flush=True)
            return {"id": user_id, "name": "", "email": ""}

    def read_channel_recent(self, channel: str, limit: int = 50) -> list[dict]:
        # User Token を優先、ダメなら Bot Token
        try:
            resp = self.search_client.conversations_history(channel=channel, limit=limit)
            return resp.get("messages", []) or []
        except SlackApiError:
            try:
                resp = self.client.conversations_history(channel=channel, limit=limit)
                return resp.get("messages", []) or []
            except SlackApiError as e:
                print(f"[read_channel error] {channel}: {e.response['error']}", flush=True)
                return []

    def post_message(self, channel: str, text: str, username: str = "") -> dict:
        kwargs = {
            "channel": channel,
            "text": text,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if username:
            # username 上書きには chat:write.customize スコープが必要
            kwargs["username"] = username
        try:
            resp = self.client.chat_postMessage(**kwargs)
            return {"ok": True, "ts": resp.get("ts")}
        except SlackApiError as e:
            print(f"[post_message error] {channel}: {e.response['error']}", flush=True)
            return {"ok": False, "error": e.response.get("error")}

    def get_channel_info(self, channel: str) -> dict:
        try:
            resp = self.client.conversations_info(channel=channel)
            return resp.get("channel", {}) or {}
        except SlackApiError as e:
            print(f"[get_channel_info error] {channel}: {e.response['error']}", flush=True)
            return {}

    def list_users(self) -> dict:
        """
        ワークスペース全ユーザを取得し、display_name/email → user_id の辞書を返す。
        - bot / 削除済みは除外
        - スコープ不足や API エラー時は空辞書を返す（呼び出し側でメンション断念）
        戻り値: {"by_name": dict, "by_email": dict}
        """
        by_name: dict[str, str] = {}
        by_email: dict[str, str] = {}
        cursor = None
        try:
            while True:
                resp = self.client.users_list(cursor=cursor, limit=200)
                for u in resp.get("members", []) or []:
                    if u.get("deleted") or u.get("is_bot"):
                        continue
                    uid = u.get("id")
                    if not uid:
                        continue
                    profile = u.get("profile", {}) or {}
                    name = (profile.get("display_name") or profile.get("real_name") or u.get("name", "")).strip()
                    email = (profile.get("email") or "").strip()
                    if name and name not in by_name:
                        by_name[name] = uid
                    if email and email not in by_email:
                        by_email[email] = uid
                cursor = (resp.get("response_metadata") or {}).get("next_cursor", "")
                if not cursor:
                    break
        except SlackApiError as e:
            print(f"[list_users error] {e.response['error']}", flush=True)
        return {"by_name": by_name, "by_email": by_email}


def _ymd(ts: int, offset_days: int = 0) -> str:
    """Unix秒 → YYYY-MM-DD（JST）。offset_days で前後にずらせる。"""
    from datetime import datetime, timezone, timedelta
    return (
        datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=9))) + timedelta(days=offset_days)
    ).strftime("%Y-%m-%d")
