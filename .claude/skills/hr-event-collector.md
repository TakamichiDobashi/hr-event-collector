---
name: hr-event-collector
description: 人事キャリア形成につながる外部イベント情報を収集してNotionに自動整理するシステムを管理するスキル。「イベント収集」「勉強会」「セミナー」「カテゴリ追加」「キーワード変更」「取得期間変更」などのキーワードで起動する。カテゴリ・キーワードの追加変更、取得期間の変更、Notionフォーマットの修正を行う。
---

# 人事キャリア イベント情報収集 管理スキル

採用・労務・組織開発・人材育成の4カテゴリについて
Connpass と X（web_search）からイベント情報を収集し、
毎週月曜日にNotionへ自動投稿するシステムの管理マニュアル。

## システム構成（Mgr型サブエージェントパターン）

```
GitHub Actions（毎週月曜 8:00 JST）
        ↓
EventDigestManager（Manager役）
        ├─ CategoryEventAgent（採用）
        │     ├─ ConnpassCollector: Connpass APIからイベント収集
        │     └─ XEventAgent: web_searchでX投稿を収集
        ├─ CategoryEventAgent（労務）
        ├─ CategoryEventAgent（組織開発）
        └─ CategoryEventAgent（人材育成）
        ↓
EventWriterAgent → Notion投稿
```

## ファイル構成

| ファイル | 役割 |
|---|---|
| `config/settings.json` | カテゴリ・キーワード・取得期間の設定 |
| `scripts/hr_event_collector.py` | メインスクリプト |
| `.github/workflows/hr-event-collector.yml` | 毎週月曜自動実行 |

## GitHub Secrets

| Secret名 | 内容 |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic APIキー |
| `NOTION_API_KEY` | Notion インテグレーショントークン |
| `NOTION_EVENTS_PAGE_ID` | 投稿先NotionページのID |

## よくある依頼と対応手順

### カテゴリを追加したい
`config/settings.json` の `categories` 配列に追加：
```json
{
  "key": "new_category",
  "name": "カテゴリ名",
  "emoji": "🎯",
  "connpass_keywords": ["キーワード1", "キーワード2"],
  "x_query": "X検索用クエリ"
}
```

### 検索キーワードを変えたい
`config/settings.json` の各カテゴリの `connpass_keywords` を編集する。

### 取得期間を変えたい
`config/settings.json` の `days_ahead` を変更する（デフォルト：30日）。

### 実行スケジュールを変えたい
`.github/workflows/hr-event-collector.yml` の `cron:` を変更する。
- 毎週月曜 8:00 JST → `cron: '0 23 * * 0'`
- 毎日 8:00 JST → `cron: '0 23 * * *'`

### エラーが出ている
よくあるエラー：
- `object_not_found` → NotionページIDの誤りorインテグレーション未接続
- Connpass 429 → レート制限（しばらく待って再実行）
- XEventAgent取得失敗 → web_searchツールの問題（Connpassの結果だけで継続）
