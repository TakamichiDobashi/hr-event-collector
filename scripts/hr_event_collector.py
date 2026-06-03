"""
人事キャリア形成 外部イベント情報 自動収集スクリプト（Google News RSS版）

採用・労務・組織開発・人材育成の各カテゴリについて
Google News RSS と X（web_search）からイベント告知を収集し、
Claudeでイベント情報を抽出してNotionにカテゴリ別整理して投稿する。

【アーキテクチャ: Mgr型サブエージェントパターン】
  EventDigestManager（Manager役）
      ├─ CategoryEventAgent × 4カテゴリ
      │     ├─ NewsEventCollector : Google News RSSからイベント告知を収集
      │     ├─ EventParserAgent   : Claudeでイベント情報を構造化抽出
      │     └─ XEventAgent        : web_searchでX投稿を収集
      └─ EventWriterAgent         : Notionに統合ページを投稿
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

import anthropic
from notion_client import Client as NotionClient

# ── 定数 ──────────────────────────────────────────────────

JST = timezone(timedelta(hours=9))
SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "../config/settings.json")

WEEKDAY_JA = {"Mon": "月", "Tue": "火", "Wed": "水",
               "Thu": "木", "Fri": "金", "Sat": "土", "Sun": "日"}


def date_label(s: str) -> str:
    for en, ja in WEEKDAY_JA.items():
        s = s.replace(en, ja)
    return s


# ── 設定読み込み ──────────────────────────────────────────

def load_settings() -> dict:
    with open(SETTINGS_PATH, encoding="utf-8") as f:
        return json.load(f)


# ── Google News RSS でイベント告知を収集 ──────────────────

class NewsEventCollector:
    """
    Google News RSSを使ってイベント告知記事を収集する。
    APIキー不要・無料で利用可能（hr-weekly-digestと同じ仕組み）。
    """

    def fetch(self, keywords: list[str], max_per_keyword: int) -> list[dict]:
        """複数キーワードでGoogle News RSSを検索し、重複除去して返す"""
        seen_urls = set()
        all_articles = []

        for keyword in keywords:
            url = (
                f"https://news.google.com/rss/search"
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

                    title       = item.findtext("title", "").strip()
                    description = item.findtext("description", "").strip()
                    description = re.sub(r"<[^>]+>", "", description)[:300]
                    pub_date    = item.findtext("pubDate", "").strip()

                    if title:
                        all_articles.append({
                            "title":       title,
                            "url":         link,
                            "description": description,
                            "pub_date":    pub_date,
                            "keyword":     keyword,
                        })

                time.sleep(0.3)

            except URLError as e:
                print(f"  Google News RSSエラー ({keyword}): {e}")
                continue

        print(f"  Google News RSS: 合計 {len(all_articles)}件の記事を収集")
        return all_articles


# ── Claude でイベント情報を抽出 ───────────────────────────

class EventParserAgent:
    """
    Google News記事からイベント告知を判定・抽出するサブエージェント。
    「これはイベント告知か？」をAIが判断し、詳細を構造化する。
    """

    SYSTEM = """あなたは人事・HR分野のイベント情報を収集する専門家です。
提供されたニュース記事の中から「今後開催される勉強会・セミナー・イベント」の
告知記事だけを選び、イベント情報を日本語で構造化して抽出してください。
必ずJSON配列のみを返し、他のテキストは含めないでください。"""

    def __init__(self, client: anthropic.Anthropic,
                 model: str = "claude-haiku-4-5-20251001"):
        self.client = client
        self.model  = model

    def parse(self, category_name: str, articles: list[dict]) -> list[dict]:
        if not articles:
            return []

        articles_text = "\n\n".join([
            f"【{i+1}】タイトル: {a['title']}\n"
            f"URL: {a['url']}\n"
            f"概要: {a['description']}"
            for i, a in enumerate(articles)
        ])

        prompt = f"""以下の記事（{len(articles)}件）の中から、
「{category_name}」分野の今後開催予定の勉強会・セミナー・イベントの告知記事を選び、
イベント情報を抽出してください。

過去に終了したイベントや、単なるニュース記事は除外してください。

## 記事一覧
{articles_text}

抽出したイベントを以下のJSON配列形式のみで返してください：
[
  {{
    "title": "イベント・勉強会のタイトル",
    "date": "開催日時（記事から読み取れる場合。不明なら空文字）",
    "format": "オンライン or オフライン or ハイブリッド or 不明",
    "location": "開催場所（オフラインの場合）",
    "organizer": "主催者・団体名",
    "summary": "イベント概要（2〜3文）",
    "url": "申し込み・詳細ページのURL",
    "fee": "参加費（無料 / 有料 / 不明）"
  }}
]

イベント告知が1件も見つからない場合は空の配列 [] を返してください。"""

        response = self.client.messages.create(
            model=self.model,
            max_tokens=2000,
            system=self.SYSTEM,
            messages=[{"role": "user", "content": prompt}]
        )

        text = response.content[0].text
        text = re.sub(r'```(?:json)?\s*', '', text).strip()

        json_match = re.search(r'\[.*\]', text, re.DOTALL)
        if json_match:
            try:
                events = json.loads(json_match.group())
                print(f"  EventParserAgent（{category_name}）: {len(events)}件のイベントを抽出")
                return events
            except json.JSONDecodeError:
                cleaned = re.sub(r',\s*([}\]])', r'\1', json_match.group())
                try:
                    return json.loads(cleaned)
                except json.JSONDecodeError:
                    pass

        print(f"  EventParserAgent（{category_name}）: 抽出できませんでした")
        return []


# ── X投稿からイベント情報を収集（web_search） ─────────────

class XEventAgent:
    """
    Anthropicのweb_searchツールを使い、
    X（旧Twitter）上のイベント告知投稿を収集するサブエージェント。
    """

    SYSTEM = """あなたは人事・HR分野のイベント情報収集の専門家です。
X（旧Twitter/x.com）で告知されている人事・HR系の勉強会・セミナー・イベント情報を探し、
日本語でわかりやすく紹介してください。
必ずJSON配列のみを返し、他のテキストは含めないでください。"""

    def __init__(self, client: anthropic.Anthropic,
                 model: str = "claude-haiku-4-5-20251001"):
        self.client = client
        self.model  = model

    def collect(self, category_name: str, query: str) -> list[dict]:
        prompt = f"""X（旧Twitter / x.com）で告知されている「{category_name}」分野の
人事・HR系の勉強会・セミナー・イベント情報を検索してください。

検索クエリ：{query}

今後開催予定のイベントを3〜5件、以下のJSON配列形式のみで返してください：
[
  {{
    "title": "イベントタイトル",
    "date": "開催日（わかる場合）",
    "format": "オンライン or オフライン or 不明",
    "summary": "イベント概要（2〜3文）",
    "url": "告知URL（あれば）",
    "source": "情報源（アカウント名など）"
  }}
]

見つからない場合は空の配列 [] を返してください。"""

        tools    = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
        messages = [{"role": "user", "content": prompt}]

        print(f"  XEventAgent（{category_name}）: web_searchで検索中...")
        text = ""

        for _ in range(5):
            try:
                response = self.client.messages.create(
                    model=self.model, max_tokens=1500,
                    system=self.SYSTEM, tools=tools, messages=messages
                )
            except Exception as e:
                print(f"  XEventAgent APIエラー: {e}")
                return []

            text_parts = [b.text for b in response.content if hasattr(b, "text")]

            if response.stop_reason == "end_turn":
                text = "".join(text_parts)
                break

            messages.append({"role": "assistant", "content": response.content})
            has_tool = any(getattr(b, "type", "") == "tool_use"
                           for b in response.content)
            if not has_tool:
                text = "".join(text_parts)
                break

        if not text:
            return []

        text = re.sub(r'```(?:json)?\s*', '', text).strip()
        json_match = re.search(r'\[.*\]', text, re.DOTALL)
        if json_match:
            try:
                posts = json.loads(json_match.group())
                print(f"  XEventAgent（{category_name}）: {len(posts)}件取得")
                return posts[:5]
            except json.JSONDecodeError:
                cleaned = re.sub(r',\s*([}\]])', r'\1', json_match.group())
                try:
                    return json.loads(cleaned)[:5]
                except json.JSONDecodeError:
                    pass

        print(f"  XEventAgent（{category_name}）: 取得できませんでした")
        return []


# ── カテゴリ担当エージェント ──────────────────────────────

class CategoryEventAgent:
    """1カテゴリのイベントを収集・抽出するサブエージェント"""

    def __init__(self, claude: anthropic.Anthropic):
        self.collector = NewsEventCollector()
        self.parser    = EventParserAgent(claude)
        self.x_agent   = XEventAgent(claude)

    def collect(self, category: dict, max_per_keyword: int) -> dict:
        name = category["name"]
        print(f"\n  [{name}] Google News RSSからイベント告知を収集中...")

        articles = self.collector.fetch(
            category["news_keywords"], max_per_keyword
        )
        events = self.parser.parse(name, articles)

        x_events = self.x_agent.collect(name, category["x_query"])

        return {
            "category": category,
            "events":   events,
            "x_events": x_events,
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

        title = (
            f"{title_prefix} "
            f"{run_dt.strftime('%Y/%m/%d')}"
        )

        total_events = sum(len(r["events"]) for r in results)
        total_x      = sum(len(r["x_events"]) for r in results)

        summary_parts = [
            f"{r['category']['emoji']} {r['category']['name']}：{len(r['events'])}件"
            for r in results
        ]

        blocks = []
        blocks.append(callout(
            "Google News から収集した人事キャリア関連 勉強会・セミナー情報\n\n"
            + "　｜　".join(summary_parts)
            + f"\n\n📰 Google News 合計 {total_events}件"
            + (f"  ＋  𝕏 {total_x}件" if total_x else ""),
            "🗓️", "green_background"
        ))
        blocks.append(divider())

        for r in results:
            cat      = r["category"]
            events   = r["events"]
            x_events = r["x_events"]

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
                    ev_title    = ev.get("title", "")
                    ev_date     = ev.get("date", "")
                    ev_format   = ev.get("format", "不明")
                    ev_location = ev.get("location", "")
                    ev_org      = ev.get("organizer", "")
                    ev_summary  = ev.get("summary", "")
                    ev_url      = ev.get("url") or None
                    ev_fee      = ev.get("fee", "不明")

                    # 見出し（タイトル）
                    blocks.append(heading(ev_title, 3))

                    # 詳細行
                    detail_parts = []
                    if ev_date:
                        detail_parts.append(f"📅 {ev_date}")
                    fmt_icon = "💻" if "オンライン" in ev_format else ("📍" if "オフライン" in ev_format else "📌")
                    detail_parts.append(f"{fmt_icon} {ev_format}")
                    if ev_location:
                        detail_parts.append(f"📍 {ev_location}")
                    if ev_fee and ev_fee != "不明":
                        detail_parts.append(f"💰 {ev_fee}")
                    if ev_org:
                        detail_parts.append(f"🏢 {ev_org}")
                    blocks.append(paragraph("　".join(detail_parts)))

                    if ev_summary:
                        blocks.append(quote_block(ev_summary))

                    if ev_url:
                        blocks.append(paragraph("🔗 詳細・申し込みはこちら", url=ev_url))

            # X情報
            if x_events:
                blocks.append(callout(
                    f"𝕏 X（Twitter）で見つかったイベント告知（{len(x_events)}件）",
                    "𝕏", "purple_background"
                ))
                for xe in x_events:
                    parts = [xe.get("title", "")]
                    if xe.get("date"):
                        parts.append(f"📅 {xe['date']}")
                    if xe.get("format") and xe["format"] != "不明":
                        parts.append(f"💻 {xe['format']}")
                    blocks.append(paragraph("　".join(filter(None, parts)), bold=True))
                    if xe.get("summary"):
                        blocks.append(paragraph(xe["summary"]))
                    if xe.get("url"):
                        blocks.append(paragraph("🔗 詳細を見る", url=xe["url"]))
                    if xe.get("source"):
                        blocks.append(paragraph(f"情報源：{xe['source']}"))

            blocks.append(divider())

        # ── Notionページ作成 ──
        response = notion.pages.create(
            parent={"page_id": parent_page_id},
            icon={"type": "emoji", "emoji": "🗓️"},
            properties={"title": {"title": [
                {"type": "text", "text": {"content": title}}
            ]}},
            children=blocks
        )
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
                              f"🗓️ {run_dt.strftime('%-m/%-d')} 更新　計{total_events}件\n"},
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
        self.claude   = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self.notion   = NotionClient(auth=os.environ["NOTION_API_KEY"])
        self.page_id  = os.environ["NOTION_EVENTS_PAGE_ID"]

    def run(self):
        now        = datetime.now(JST)
        max_arts   = self.settings.get("max_articles_per_keyword", 10)
        categories = self.settings["categories"]

        print(f"=== 人事イベント収集開始: {now.strftime('%Y/%m/%d %H:%M')} ===")
        print(f"    対象カテゴリ: {len(categories)}個")

        agent   = CategoryEventAgent(self.claude)
        results = []

        for cat in categories:
            result = agent.collect(cat, max_arts)
            results.append(result)

        print(f"\n[EventWriterAgent] Notionに投稿中...")
        writer = EventWriterAgent()
        info   = writer.post(
            self.notion, self.page_id, results, now,
            self.settings["notion"]["title_prefix"]
        )

        print(f"\n=== 完了 ===")
        print(f"Notionページ: {info['url']}")


# ── エントリーポイント ─────────────────────────────────────

if __name__ == "__main__":
    required = ["ANTHROPIC_API_KEY", "NOTION_API_KEY", "NOTION_EVENTS_PAGE_ID"]
    missing  = [v for v in required if not os.environ.get(v)]
    if missing:
        raise EnvironmentError(f"環境変数が未設定: {', '.join(missing)}")

    EventDigestManager().run()
