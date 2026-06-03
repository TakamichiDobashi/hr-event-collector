"""
人事キャリア形成 外部イベント情報 自動収集スクリプト（Python抽出版）

Google News RSS からイベント告知を収集し、
Pythonのキーワードマッチング・正規表現でイベント情報を抽出。
Claude API 不要のため、ネットワーク制限に左右されず安定稼働。

【アーキテクチャ】
  EventDigestManager（Manager役）
      ├─ CategoryEventAgent × 4カテゴリ
      │     ├─ NewsEventCollector  : Google News RSSでイベント告知を収集
      │     └─ EventArticleFilter  : Pythonでイベント情報を抽出・構造化
      └─ EventWriterAgent          : Notionにカテゴリ別整理して投稿
"""

import os
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from urllib.request import urlopen, Request
from urllib.error import URLError

from notion_client import Client as NotionClient

# ── 定数 ──────────────────────────────────────────────────

JST = timezone(timedelta(hours=9))
SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "../config/settings.json")

WEEKDAY_JA = {"Mon": "月", "Tue": "火", "Wed": "水",
               "Thu": "木", "Fri": "金", "Sat": "土", "Sun": "日"}

# イベント告知を示すキーワード
EVENT_KEYWORDS = [
    "勉強会", "セミナー", "ウェビナー", "webinar", "イベント",
    "参加者募集", "申し込み", "お申し込み", "開催", "開催決定",
    "ワークショップ", "フォーラム", "カンファレンス", "講演会",
    "座談会", "交流会", "研修", "無料セミナー", "参加無料",
]

# オンライン開催を示すキーワード
ONLINE_KEYWORDS = [
    "オンライン", "zoom", "Zoom", "Teams", "Meet",
    "ウェビナー", "webinar", "web開催", "オンライン開催",
]

# 日付パターン（優先度順）
DATE_PATTERNS = [
    r'\d{4}年\s*\d{1,2}月\s*\d{1,2}日',   # 2026年6月19日
    r'\d{1,2}月\s*\d{1,2}日',              # 6月19日
    r'\d{1,2}/\d{1,2}',                     # 6/19
    r'\d{4}-\d{2}-\d{2}',                   # 2026-06-19
]


# ── 設定読み込み ──────────────────────────────────────────

def load_settings() -> dict:
    with open(SETTINGS_PATH, encoding="utf-8") as f:
        return json.load(f)


# ── Google News RSS でイベント告知を収集 ──────────────────

class NewsEventCollector:
    """
    Google News RSSを使ってイベント告知記事を収集する。
    APIキー不要・無料・Claude不要で安定稼働。
    """

    def fetch(self, keywords: list[str], max_per_keyword: int) -> list[dict]:
        seen_urls = set()
        all_articles = []

        for keyword in keywords:
            url = (
                "https://news.google.com/rss/search"
                f"?q={quote(keyword)}&hl=ja&gl=JP&ceid=JP:ja"
            )
            try:
                req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urlopen(req, timeout=10) as resp:
                    tree = ET.parse(resp)

                items = tree.findall(".//item")[:max_per_keyword]
                for item in items:
                    link = item.findtext("link", "").strip()
                    if link in seen_urls:
                        continue
                    seen_urls.add(link)

                    title = item.findtext("title", "").strip()
                    desc  = item.findtext("description", "").strip()
                    desc  = re.sub(r"<[^>]+>", "", desc)[:300]
                    pub   = item.findtext("pubDate", "").strip()

                    if title:
                        all_articles.append({
                            "title":       title,
                            "url":         link,
                            "description": desc,
                            "pub_date":    pub,
                            "keyword":     keyword,
                        })

                time.sleep(0.3)

            except URLError as e:
                print(f"  RSS取得エラー ({keyword}): {e}")

        return all_articles


# ── Python だけでイベント情報を抽出・構造化 ───────────────

class EventArticleFilter:
    """
    Google News記事をPythonのキーワードマッチング・正規表現で
    イベント告知に絞り込み、構造化する。Claude不要。
    """

    def filter_and_extract(self, articles: list[dict],
                            category_name: str) -> list[dict]:
        events = []
        seen_titles = set()

        for article in articles:
            title = article["title"]
            desc  = article["description"]
            url   = article["url"]
            text  = title + " " + desc

            # ── イベントキーワードが含まれるか判定 ──
            if not any(kw in text for kw in EVENT_KEYWORDS):
                continue

            # タイトル重複除去
            title_key = re.sub(r'\s+', '', title)[:30]
            if title_key in seen_titles:
                continue
            seen_titles.add(title_key)

            # ── 日付を抽出 ──
            detected_date = ""
            for pattern in DATE_PATTERNS:
                m = re.search(pattern, text)
                if m:
                    detected_date = m.group()
                    break

            # ── 開催形式を判定 ──
            text_lower = text.lower()
            if any(kw.lower() in text_lower for kw in ONLINE_KEYWORDS):
                event_format = "オンライン"
            else:
                event_format = "不明"

            # ── 参加費を判定 ──
            if any(kw in text for kw in ["無料", "参加費無料", "参加無料", "free"]):
                fee = "無料"
            elif any(kw in text for kw in ["有料", "参加費", "円", "¥"]):
                fee = "有料"
            else:
                fee = "不明"

            # ── タイトルから媒体名を除去（ - SOURCE の部分） ──
            clean_title = re.sub(r'\s*[-–—]\s*[^\-–—]+$', '', title).strip()

            events.append({
                "title":   clean_title or title,
                "date":    detected_date,
                "format":  event_format,
                "summary": desc[:200] if desc else "",
                "url":     url,
                "fee":     fee,
            })

        print(f"  EventArticleFilter（{category_name}）: "
              f"{len(articles)}件中 {len(events)}件がイベント告知")
        return events


# ── カテゴリ担当エージェント ──────────────────────────────

class CategoryEventAgent:
    """1カテゴリのイベントを収集・抽出するサブエージェント"""

    def __init__(self):
        self.collector = NewsEventCollector()
        self.extractor = EventArticleFilter()

    def collect(self, category: dict, max_per_keyword: int) -> dict:
        name = category["name"]
        print(f"\n  [{name}] Google News RSSからイベント告知を収集中...")

        articles = self.collector.fetch(
            category["news_keywords"], max_per_keyword
        )
        print(f"  [{name}] RSS取得: {len(articles)}件の記事")

        events = self.extractor.filter_and_extract(articles, name)

        return {
            "category": category,
            "events":   events,
        }


# ── Notion に投稿 ─────────────────────────────────────────

class EventWriterAgent:

    def post(self, notion: NotionClient, parent_page_id: str,
             results: list[dict], run_dt: datetime,
             title_prefix: str) -> dict:

        def callout(text, emoji, color):
            return {
                "object": "block", "type": "callout",
                "callout": {
                    "rich_text": [{"type": "text", "text": {"content": text}}],
                    "icon": {"emoji": emoji}, "color": color
                }
            }

        def heading(text, level=2):
            t = f"heading_{level}"
            return {"object": "block", "type": t,
                    t: {"rich_text": [{"type": "text",
                        "text": {"content": text}}]}}

        def paragraph(text, url=None, bold=False):
            rich = {"type": "text", "text": {"content": text}}
            if url:
                rich["text"]["link"] = {"url": url}
            if bold:
                rich["annotations"] = {"bold": True}
            return {"object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": [rich]}}

        def quote_block(text):
            return {"object": "block", "type": "quote",
                    "quote": {"rich_text": [{"type": "text",
                              "text": {"content": text}}]}}

        def divider():
            return {"object": "block", "type": "divider", "divider": {}}

        MAX_PER_CAT = 15   # カテゴリごとの最大表示件数
        CHUNK_SIZE  = 90   # Notion APIの1回あたりの上限（余裕を持って90）

        title = f"{title_prefix} {run_dt.strftime('%Y/%m/%d')}"

        # 各カテゴリの表示件数を制限
        for r in results:
            r["events"] = r["events"][:MAX_PER_CAT]

        total = sum(len(r["events"]) for r in results)
        summary_parts = [
            f"{r['category']['emoji']} {r['category']['name']}："
            f"{len(r['events'])}件"
            for r in results
        ]

        blocks = []
        blocks.append(callout(
            "Google News から収集した人事キャリア関連 勉強会・セミナー情報\n\n"
            + "　｜　".join(summary_parts)
            + f"\n\n📰 合計 {total}件（各カテゴリ最大{MAX_PER_CAT}件表示）",
            "🗓️", "green_background"
        ))
        blocks.append(divider())

        for r in results:
            cat    = r["category"]
            events = r["events"]

            blocks.append(heading(
                f"{cat['emoji']} {cat['name']}　（{len(events)}件）", 2
            ))

            if not events:
                blocks.append(callout(
                    "今週は該当するイベント情報が見つかりませんでした。",
                    "📭", "gray_background"
                ))
            else:
                for ev in events:
                    blocks.append(heading(ev["title"], 3))

                    detail = []
                    if ev["date"]:
                        detail.append(f"📅 {ev['date']}")
                    fmt_icon = "💻" if ev["format"] == "オンライン" else "📌"
                    detail.append(f"{fmt_icon} {ev['format']}")
                    if ev["fee"] != "不明":
                        detail.append(f"💰 {ev['fee']}")
                    blocks.append(paragraph("　".join(detail)))

                    if ev["summary"]:
                        blocks.append(quote_block(ev["summary"]))

                    if ev["url"]:
                        blocks.append(paragraph("🔗 詳細・申し込みはこちら",
                                                 url=ev["url"]))

            blocks.append(divider())

        # Notionページ作成（最初の CHUNK_SIZE ブロックのみ）
        first_chunk = blocks[:CHUNK_SIZE]
        remaining   = blocks[CHUNK_SIZE:]

        response = notion.pages.create(
            parent={"page_id": parent_page_id},
            icon={"type": "emoji", "emoji": "🗓️"},
            properties={"title": {"title": [
                {"type": "text", "text": {"content": title}}
            ]}},
            children=first_chunk
        )
        page_id  = response["id"]
        page_url = response["url"]

        # 残りのブロックを CHUNK_SIZE ずつ追加
        for i in range(0, len(remaining), CHUNK_SIZE):
            chunk = remaining[i:i + CHUNK_SIZE]
            notion.blocks.children.append(block_id=page_id, children=chunk)
            time.sleep(0.3)
        print(f"  ページ作成完了（合計 {len(blocks)} ブロック）")
        page_id  = response["id"]
        page_url = response["url"]

        # 親ページのインデックスに追加
        all_blocks = notion.blocks.children.list(
            block_id=parent_page_id).get("results", [])
        heading_id = None
        for block in all_blocks:
            if block.get("type") == "heading_2":
                texts = block["heading_2"].get("rich_text", [])
                content = "".join(
                    t.get("text", {}).get("content", "") for t in texts)
                if "一覧" in content or "アーカイブ" in content:
                    heading_id = block["id"]
                    break

        entry = {
            "object": "block", "type": "callout",
            "callout": {
                "rich_text": [
                    {"type": "text",
                     "text": {"content":
                              f"🗓️ {run_dt.strftime('%-m/%-d')} 更新　"
                              f"計{total}件\n"},
                     "annotations": {"bold": True}},
                    {"type": "mention",
                     "mention": {"type": "page", "page": {"id": page_id}}}
                ],
                "icon": {"emoji": "🗓️"}, "color": "default"
            }
        }
        kwargs = {"children": [entry]}
        if heading_id:
            kwargs["after"] = heading_id
        notion.blocks.children.append(block_id=parent_page_id, **kwargs)

        return {"url": page_url, "id": page_id}


# ── Manager ───────────────────────────────────────────────

class EventDigestManager:

    def __init__(self):
        self.settings = load_settings()
        self.notion   = NotionClient(auth=os.environ["NOTION_API_KEY"])
        self.page_id  = os.environ["NOTION_EVENTS_PAGE_ID"]

    def run(self):
        now        = datetime.now(JST)
        max_arts   = self.settings.get("max_articles_per_keyword", 10)
        categories = self.settings["categories"]

        print(f"=== 人事イベント収集開始: {now.strftime('%Y/%m/%d %H:%M')} ===")
        print(f"    対象カテゴリ: {len(categories)}個")

        agent   = CategoryEventAgent()
        results = []

        for cat in categories:
            result = agent.collect(cat, max_arts)
            results.append(result)

        total = sum(len(r["events"]) for r in results)
        print(f"\n合計 {total}件のイベントを収集")

        print("\n[EventWriterAgent] Notionに投稿中...")
        writer = EventWriterAgent()
        info   = writer.post(
            self.notion, self.page_id, results, now,
            self.settings["notion"]["title_prefix"]
        )

        print(f"\n=== 完了 ===")
        print(f"Notionページ: {info['url']}")


# ── エントリーポイント ─────────────────────────────────────

if __name__ == "__main__":
    required = ["NOTION_API_KEY", "NOTION_EVENTS_PAGE_ID"]
    missing  = [v for v in required if not os.environ.get(v)]
    if missing:
        raise EnvironmentError(f"環境変数が未設定: {', '.join(missing)}")

    EventDigestManager().run()
