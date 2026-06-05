# 提案機会検知くん（議事録BOT統合版）

ナイル株式会社の社内Slackで議事録転送Bot（`U0B305165M1`）が各案件チャンネルに投稿する議事録から、**アップセル機会**と**受注済み案件のナイル側TODO**を検知し、通知チャンネル（`#dxm_提案機会_検知くん`）に送信＋スプレッドシートに記録するBot。

検知くんファミリーのうち**ポジティブ系**（提案機会）を担当。ネガティブ系（クレーム検知・解約リスク検知）は [Claim_check](https://github.com/risaki-iha/Claim_check) リポ。

## 設計の重心

- **主役は 🟢🟡 アップセル機会検知**。マネ＋AMが取りこぼさないようにする
- 🔵 ナイル側TODOは副次的価値（記録目的）
- 凡庸な「全TODOを通知」ではなく、**重要シグナルをタグで色分け**して伝える

## 分類タグ

| タグ | 対象 |
|---|---|
| 🟢 | アップセル機会（新規領域・追加提案・PoC・フェーズ1・来期推進） |
| 🟡 | 競争・リスク警告（コンペ・RFP・再提案・複数社提案） |
| 🔵 | ナイル側TODO |
| ⚪ | 顧客アクション参考 |
| 🔥 | 早急（追加付与） |

1案件に複数タグOK。

## 初版スコープ（v1）

- ✅ 議事録Bot投稿スキャン
- ✅ 議事録パース（▼決定事項/▼タスク/▼メモ）
- ✅ 社外/社内振り分け（コール名プレフィクス3段階判定）
- ✅ 期日抽出（A型確定日 / B型原文）
- ✅ AI判定（Claude API、分類タグ＋サマリ生成）
- ✅ Slack通知（スリム版5行、**メンションなし版**）
- ✅ スプシ書き込み（14列、項目単位）

## 別フェーズ予定

- 🔜 PR2: マネ＋AMメンション機能（`supervisor_map` 流用）
- 🔜 PR3: 期日リマインダー（A型 3日前/1日前）

## ファイル構成

```
Teian_kenchi/
├ scripts/
│  ├ lib/
│  │  ├ teian_kenchi.py         # コア実装（パース・タグ判定・期日抽出・通知整形）
│  │  ├ teian_sheets_tools.py   # スプシ書き込み（14列）
│  │  ├ supervisor_map.py       # マネ＋AM解決（Claim_checkからコピー、PR2で使用）
│  │  ├ slack_tools.py          # Slack API ラッパー（Claim_checkからコピー）
│  │  ├ claude_oauth.py         # Claude API認証（Claim_checkからコピー）
│  │  └ __init__.py
│  ├ run_teian_kenchi.py        # エントリポイント
│  └ smoke_test_teian_kenchi.py # パース部スモークテスト（API不要）
├ skills/
│  └ teian-kenchi-realtime.md   # AI判定プロンプト（system に投入）
├ .github/
│  └ workflows/
│     └ teian_kenchi.yml        # workflow_dispatch（cron-job.org連携前提）
├ requirements.txt
├ README.md
└ CLAUDE.md                     # 自動実行用注釈
```

## 動作の流れ

1. **検知スキャン**：cron-job.org が GitHub Actions の `workflow_dispatch` を JST 11/13/15/17/19 時に叩く
2. **議事録Bot投稿スキャン**：`from:<@U0B305165M1>` でSlack検索、前回スキャン以降の新規投稿を取得
3. **議事録パース**：「要約:」以降「関係構築・心理的距離:」直前までを抽出 → ▼決定事項 / ▼タスク / ▼メモ に分解
4. **社外/社内振り分け**：コール名のプレフィクス（【社外】/【社内】/【確定】/AI判定）で3段階判定
5. **期日抽出**：A型（確定日）は YYYY-MM-DD で正規化、B型（曖昧・相対・状態）は原文保存
6. **AI判定**：Claude API（`skills/teian-kenchi-realtime.md` を system に投入）で分類タグ＋サマリ生成
7. **Slack通知**：スリム版5行で `#dxm_提案機会_検知くん` に投稿
8. **スプシ書き込み**：「AI検知ログ」シートに項目1件＝1行で追記

## 必要な GitHub Secrets

| Secret | 用途 |
|---|---|
| `CLAUDE_REFRESH_TOKEN` | Claude API OAuth |
| `ANTHROPIC_API_KEY` | Claude API |
| `SLACK_BOT_TOKEN` | Slack 投稿・ユーザー取得 |
| `SLACK_USER_TOKEN` | Slack search.messages（User Token 必須） |
| `GOOGLE_SHEETS_KEY` | スプシ書き込み（サービスアカウント JSON） |
| `PAT_FOR_SECRETS` | CLAUDE_REFRESH_TOKEN 自動ローテのため |

## 関連スプシ・チャンネル

- 通知先：`#dxm_提案機会_検知くん`（`C0AHUC1VDDK`）
- スプシ：[1UvbEP78... / 「AI検知ログ」シート](https://docs.google.com/spreadsheets/d/1UvbEP78vi10WttBcapWI9_Dpe9nvd7YHv2TLdiemIsA/edit?gid=731755367)
- データソース：議事録転送Bot（`U0B305165M1`、display_name: `minutes-forward-bot`）
- マネ＋AM マスタ（PR2 で使用）：`1PWqW08yD6shJu5sRUxTZvf7w7K7TaJEuQcDU2QmkZXY` の「1_顧客一覧」シート

## 動作確認

ローカルスモークテスト（パース部のみ・API不要）：

```bash
python scripts/smoke_test_teian_kenchi.py
```

サンプル9件（`C:\Users\risaki_iha\projects\teian-kenchi\samples\`）を読んで、社外/社内判定・期日抽出・セクションパースを検証する。

## 設計メモ

詳細な設計は `C:\Users\risaki_iha\projects\teian-kenchi\CLAUDE.md` を参照（10セクション + 7-A初版スコープ + サンプル9件）。
