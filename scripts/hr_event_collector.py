"""
人事キャリア形成 外部イベント情報 自動収集スクリプト

採用・労務・組織開発・人材育成の各カテゴリについて
Connpass API と X（web_search）からイベント情報を収集し
Notion にカテゴリ別整理して投稿する。

【アーキテクチャ: Mgr型サブエージェントパターン】
  EventDigestManager
      ├─ CategoryEventAgent × 4カテゴリ（並列的に処理）
      │     ├─ ConnpassCollector  : Connpass APIからイベント収集
      │     └─ XEventAgent        : X/web_searchからイベント情報収集
      └─ EventWriterAgent         : Notionに統合ページを投稿
"""

import os
import json
import re
import time
import requests
from datetime import datetime, timedelta, timezone

import anthropic
from notion_client import Client as NotionClient

# ── 定数 ──────────────────────────────────────────────────

JST = timezone(timedelta(hours=9))
CONNPASS_API = "https://connpass.com/api/v2/event/"
SETTINGS_PATH = os.path.join(os.path.dirname(__file__), "../config/settings.json")

WEEKDAY_JA = {"Mon": "月", "Tue": "火", "Wed": "水",
               "Thu": "木", "Fri": "金", "Sat": "土", "Sun": "日"}


def date_label(dt: datetime) -> str:
    s = dt.strftime("%-m/%-d（%a）")
    for en, ja in WEEKDAY_JA.items():
        s = s.replace(en, ja)
    return s


# ── 設定読み込み ──────────────────────────────────────────

def load_settings() -> dict:
    with open(SETTINGS_PATH, encoding="utf-8") as f:
        return json.load(f)


# ── Connpass API でイベントを収集 ─────────────────────────

class ConnpassCollector:
    """
    Connpass APIを使って人事系イベントを収集するコレクター。
    APIキー不要・無料で利用可能。
    """

    def fetch(self, keywords: list[str], days_ahead: int,
              max_per_keyword: int) -> list[dict]:
        """複数キーワードでイベントを検索し、重複を除去して返す"""
        today = datetime.now(JST)
        end_date = today + timedelta(days=days_ahead)
        ymd_from = today.strftime("%Y%m%d")
        ymd_to = end_date.strftime("%Y%m%d")

        seen_ids = set()
        all_events = []

        for keyword in keywords:
            try:
                resp = requests.get(
                    CONNPASS_API,
                    params={
                        "keyword": keyword,
                        "ymd_from": ymd_from,
                        "ymd_to": ymd_to,
                        "count": max_per_keyword,
                        "order": 2,  # 開催日順
                    },
                    timeout=10
                )
                if resp.status_code != 200:
                    print(f"  Connpass APIエラー: {resp.status_code} (keyword={keyword})")
                    continue

                data = resp.json()
                for event in data.get("events", []):
                    eid = event.get("event_id")
                    if eid and eid not in seen_ids:
                        seen_ids.add(eid)
                        all_events.append(self._normalize(event))

                time.sleep(0.3)  # レート制限対応

            except Exception as e:
                print(f"  Connpass取得エラー ({keyword}): {e}")
                continue

        # 開催日順にソート
        all_events.sort(key=lambda e: e["started_at"])
        return all_events

    def _normalize(self, raw: dict) -> dict:
        """APIレスポンスを扱いやすい形に整形する"""
        started_raw = raw.get("started_at", "")
        started_dt = None
        if started_raw:
            try:
                started_dt = datetime.fromisoformat(
                    started_raw.replace("Z", "+00:00")).astimezone(JST)
            except ValueError:
                pass

        place = raw.get("place", "") or ""
        address = raw.get("address", "") or ""
        is_online = any(w in (place + address).lower()
                        for w in ["online", "オンライン", "zoom", "teams", "meet"])

        return {
            "event_id":     raw.get("event_id"),
            "title":        raw.get("title", ""),
            "url":          raw.get("event_url", ""),
            "started_at":   started_raw,
            "started_dt":   started_dt,
            "date_label":   date_label(started_dt) if started_dt else "日時未定",
            "place":        place or ("オンライン" if is_online else ""),
            "is_online":    is_online,
            "description":  (raw.get("description") or "")[:300].strip(),
            "catch":        (raw.get("catch") or "").strip(),
            "limit":        raw.get("limit", 0),
            "accepted":     raw.get("accepted", 0),
            "owner_name":   raw.get("owner_display_name", ""),
        }


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
        self.model = model

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
    "url": "告知URL or 投稿URL（あれば）",
    "source": "情報源（アカウント名など）"
  }}
]

見つからない場合は空の配列 [] を返してください。"""

        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
        messages = [{"role": "user", "content": prompt}]

        print(f"  XEventAgent（{category_name}）: web_searchで検索中...")
        text = ""

        for _ in range(5):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=1500,
                    system=self.SYSTEM,
                    tools=tools,
                    messages=messages
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
    """
    1カテゴリのイベントを収集するサブエージェント。
    Connpass + X両方から情報を集める。
    """

    def __init__(self, claude: anthropic.Anthropic):
        self.connpass = ConnpassCollector()
        self.x_agent  = XEventAgent(claude)

    def collect(self, category: dict, days_ahead: int,
                max_per_keyword: int) -> dict:
        name = category["name"]
        print(f"\n  [{name}] Connpass からイベント収集中...")
        connpass_events = self.connpass.fetch(
            category["connpass_keywords"], days_ahead, max_per_keyword
        )
        print(f"  [{name}] Connpass: {len(connpass_events)}件")

        x_events = self.x_agent.collect(name, category["x_query"])

        return {
            "category":        category,
            "connpass_events": connpass_events,
            "x_events":        x_events,
        }


# ── Notion に投稿 ─────────────────────────────────────────

class EventWriterAgent:
    """収集したイベント情報をNotionにカテゴリ別整理して投稿するエージェント"""

    def post(self, notion: NotionClient, parent_page_id: str,
             results: list[dict], run_dt: datetime,
             title_prefix: str) -> dict:

        # ── Notionブロックのヘルパー ──
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
                    t: {"rich_text": [{"type": "text", "text": {"content": text}}]}}

        def paragraph(text, url=None, bold=False):
            rich = {"type": "text", "text": {"content": text}}
            if url:
                rich["text"]["link"] = {"url": url}
            if bold:
                rich["annotations"] = {"bold": True}
            return {"object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": [rich]}}

        def quote(text):
            return {"object": "block", "type": "quote",
                    "quote": {"rich_text": [{"type": "text",
                              "text": {"content": text}}]}}

        def divider():
            return {"object": "block", "type": "divider", "divider": {}}

        # ── タイトル・サマリー ──
        date_str = run_dt.strftime("%Y/%m/%d")
        end_date = (run_dt + timedelta(days=30)).strftime("%-m/%-d")
        title    = f"{title_prefix} {date_str}"

        summary_parts = []
        total_connpass = 0
        for r in results:
            cnt = len(r["connpass_events"])
            total_connpass += cnt
            summary_parts.append(f"{r['category']['emoji']} {r['category']['name']}：{cnt}件")

        blocks = []
        summary_text = (
            f"今後30日間（〜{end_date}）の人事キャリア関連イベント\n\n"
            + "　｜　".join(summary_parts)
            + f"\n\n✅ Connpass 合計 {total_connpass}件 ＋ 𝕏 投稿情報"
        )
        blocks.append(callout(summary_text, "🗓️", "green_background"))
        blocks.append(divider())

        # ── カテゴリ別セクション ──
        for r in results:
            cat      = r["category"]
            c_events = r["connpass_events"]
            x_events = r["x_events"]

            blocks.append(heading(
                f"{cat['emoji']} {cat['name']}　（Connpass {len(c_events)}件）", 2
            ))

            # Connpassイベント
            if not c_events:
                blocks.append(callout("今後30日間のイベントは見つかりませんでした。",
                                      "📭", "gray_background"))
            else:
                for ev in c_events:
                    # イベントカード
                    loc_icon = "💻" if ev["is_online"] else "📍"
                    loc_text = ev["place"] or ("オンライン" if ev["is_online"] else "")
                    limit_text = ""
                    if ev["limit"] and ev["accepted"] is not None:
                        remaining = ev["limit"] - ev["accepted"]
                        limit_text = f"　　残り {remaining}/{ev['limit']}席"

                    detail_line = (
                        f"📅 {ev['date_label']}"
                        + (f"　{loc_icon} {loc_text}" if loc_text else "")
                        + (f"　👤 {ev['owner_name']}" if ev["owner_name"] else "")
                        + limit_text
                    )

                    blocks.append(heading(ev["title"], 3))
                    blocks.append(paragraph(detail_line))
                    if ev["catch"]:
                        blocks.append(quote(ev["catch"]))
                    if ev["url"]:
                        blocks.append(paragraph("🔗 Connpass で詳細・申し込み", url=ev["url"]))

            # X イベント情報
            if x_events:
                blocks.append(callout(
                    f"𝕏 X（Twitter）で見つかったイベント情報（{len(x_events)}件）",
                    "𝕏", "purple_background"
                ))
                for xe in x_events:
                    xe_title = xe.get("title", "")
                    xe_date  = xe.get("date", "")
                    xe_fmt   = xe.get("format", "")
                    xe_sum   = xe.get("summary", "")
                    xe_url   = xe.get("url") or None
                    xe_src   = xe.get("source", "")

                    header_parts = [xe_title]
                    if xe_date:
                        header_parts.append(f"📅 {xe_date}")
                    if xe_fmt and xe_fmt != "不明":
                        header_parts.append(f"💻 {xe_fmt}" if "オンライン" in xe_fmt else f"📍 {xe_fmt}")

                    blocks.append(paragraph("　".join(header_parts), bold=True))
                    if xe_sum:
                        blocks.append(paragraph(xe_sum))
                    if xe_url:
                        blocks.append(paragraph("🔗 詳細・告知を見る", url=xe_url))
                    if xe_src:
                        blocks.append(paragraph(f"情報源：{xe_src}"))

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
                content = "".join(t.get("text", {}).get("content", "")
                                  for t in texts)
                if "一覧" in content or "アーカイブ" in content:
                    heading_id = block["id"]
                    break

        date_label_str = run_dt.strftime("%-m/%-d")
        entry = {
            "object": "block", "type": "callout",
            "callout": {
                "rich_text": [
                    {"type": "text",
                     "text": {"content": f"🗓️ {date_label_str} 更新　計{total_connpass}件\n"},
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
    """Mgr型: 各カテゴリのエージェントを統括し結果をNotionに投稿する"""

    def __init__(self):
        self.settings = load_settings()
        self.claude   = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self.notion   = NotionClient(auth=os.environ["NOTION_API_KEY"])
        self.page_id  = os.environ["NOTION_EVENTS_PAGE_ID"]

    def run(self):
        now        = datetime.now(JST)
        days_ahead = self.settings.get("days_ahead", 30)
        max_evts   = self.settings.get("max_events_per_keyword", 10)
        categories = self.settings["categories"]

        print(f"=== 人事イベント収集開始: {now.strftime('%Y/%m/%d %H:%M')} ===")
        print(f"    対象カテゴリ: {len(categories)}個  取得期間: {days_ahead}日間")

        agent   = CategoryEventAgent(self.claude)
        results = []

        for cat in categories:
            result = agent.collect(cat, days_ahead, max_evts)
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
