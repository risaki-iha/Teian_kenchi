"""
Slack Bot Tokenのスコープ確認（読み取り専用・投稿なし）。
auth.test を叩き、レスポンスヘッダー x-oauth-scopes を出力するだけ。
chat:write.customize が付与済みか確認するための一回性スクリプト。
"""

import os

from lib.slack_tools import SlackTools


def main() -> None:
    slack = SlackTools()
    resp = slack.client.auth_test()
    scopes_header = resp.headers.get("x-oauth-scopes", "")
    scopes = [s.strip() for s in scopes_header.split(",") if s.strip()]
    print(f"[auth.test] user={resp.get('user')} bot_id={resp.get('bot_id')}", flush=True)
    print(f"[scopes] {scopes}", flush=True)
    print(f"[chat:write.customize] {'chat:write.customize' in scopes}", flush=True)


if __name__ == "__main__":
    main()
