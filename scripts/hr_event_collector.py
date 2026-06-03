"""
人事キャリア形成 外部イベント情報 自動収集スクリプト（日付フィルタ・UI強化版）

改善点：
  - 今日〜1ヶ月以内のイベントのみ表示（過去日除外）
  - 日付をColor Calloutで目立たせ、「あと○日」を表示
  - 近日順にソート・色分け（7日以内=赤、14日=橙、21日=黄、30日=緑）
  - 洗練されたカードデザイン
"""

import os
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone, date as date_type
from urllib.parse import quote
from urllib.request import urlopen, Request
from urllib.error import URLError
from typing import Optional

import requests
from notion_client import Client as NotionClient

# ── 定数 ──────────────────────────────────────────────────

JST            = timezone(timedelta(hours=9))
SETTINGS_PATH  = os.path.join(os.path.dirname(__file__), "../config/settings.json")
DAYS_AHEAD     = 30

WEEKDAY_JA = {"Monday": "月", "Tuesday": "火", "Wednesday": "水",
              "Thursday": "木", "Friday": "金", "Saturday": "土", "Sunday": "日",
              "Mon": "月", "Tue": "火", "Wed": "水",
              "Thu": "木", "Fri": "金", "Sat": "土", "Sun": "日"}

EVENT_KEYWORDS = [
    "勉強会", "セミナー", "ウェビナー", "webinar", "イベント",
    "参加者募集", "申し込み", "お申し込み", "開催",
    "ワークショップ", "フォーラム", "カンファレンス", "講演会",
    "座談会", "交流会", "研修", "無料セミナー", "参加無料",
]

ONLINE_KEYWORDS = [
    "オンライン", "zoom", "Zoom", "Teams", "Meet",
    "ウェビナー", "webinar", "web開催",
]

# 関東圏のキーワード（これが含まれるオフラインイベントは掲載）
KANTO_KEYWORDS = [
    # 都道府県
    "東京", "神奈川", "埼玉", "千葉", "茨城", "群馬", "栃木",
    # 主要エリア・駅周辺
    "渋谷", "新宿", "六本木", "秋葉原", "銀座", "丸の内", "有楽町",
    "品川", "恵比寿", "目黒", "池袋", "上野", "浅草", "赤坂",
    "青山", "表参道", "原宿", "代官山", "大手町", "日本橋",
    "虎ノ門", "霞が関", "永田町", "汐留", "浜松町",
    "横浜", "川崎", "さいたま", "千葉市", "柏", "松戸",
    "人形町",   # 人事図書館の所在地
]

# 人事図書館イベントを識別するキーワード（無条件で掲載）
JINJITOSHOKAN_KEYWORDS = ["人事図書館", "人事図書館メンバー", "HRライブラリー"]

DATE_PATTERNS = [
    (r'(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日', "ymd_full"),
    (r'(\d{1,2})月\s*(\d{1,2})日',              "md"),
    (r'(\d{1,2})/(\d{1,2})',                     "slash"),
    (r'(\d{4})-(\d{2})-(\d{2})',                 "iso"),
]


# ── 設定読み込み ──────────────────────────────────────────

def load_settings() -> dict:
    with open(SETTINGS_PATH, encoding="utf-8") as f:
        return json.load(f)


# ── 日付ユーティリティ ────────────────────────────────────

def parse_japanese_date(text: str, ref_year: int) -> Optional[date_type]:
    """日本語・スラッシュ・ISO形式の日付文字列をdateオブジェクトに変換する"""
    for pattern, kind in DATE_PATTERNS:
        m = re.search(pattern, text)
        if not m:
            continue
        try:
            if kind == "ymd_full":
                return date_type(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            elif kind in ("md", "slash"):
                mo, day = int(m.group(1)), int(m.group(2))
                d = date_type(ref_year, mo, day)
                # 既に過去なら翌年と判断
                if d < date_type.today():
                    d = date_type(ref_year + 1, mo, day)
                return d
            elif kind == "iso":
                return date_type(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
    return None


def days_until(d: date_type) -> int:
    return (d - date_type.today()).days


def date_display(d: date_type) -> str:
    """'6月19日（木）' 形式"""
    weekday = d.strftime("%A")
    ja_wd   = WEEKDAY_JA.get(weekday, weekday[:3])
    return f"{d.month}月{d.day}日（{ja_wd}）"


def date_color(days: int) -> str:
    """日数に応じたNotionのcallout背景色"""
    if days <= 7:   return "red_background"
    if days <= 14:  return "orange_background"
    if days <= 21:  return "yellow_background"
    return "green_background"


# ── Google News RSS でイベント告知を収集 ──────────────────

class NewsEventCollector:

    def fetch(self, keywords: list[str], max_per_keyword: int) -> list[dict]:
        seen_urls  = set()
        all_items  = []

        for keyword in keywords:
            url = (
                "https://news.google.com/rss/search"
                f"?q={quote(keyword)}&hl=ja&gl=JP&ceid=JP:ja"
            )
            try:
                req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urlopen(req, timeout=10) as resp:
                    tree = ET.parse(resp)

                for item in tree.findall(".//item")[:max_per_keyword]:
                    link = item.findtext("link", "").strip()
                    if link in seen_urls:
                        continue
                    seen_urls.add(link)

                    title = item.findtext("title", "").strip()
                    desc  = re.sub(r"<[^>]+>", "",
                                   item.findtext("description", ""))[:300].strip()
                    pub   = item.findtext("pubDate", "").strip()
                    if title:
                        all_items.append({
                            "title": title, "url": link,
                            "description": desc, "pub_date": pub,
                        })
                time.sleep(0.3)

            except URLError as e:
                print(f"  RSS取得エラー: {e}")

        return all_items


# ── Pythonでイベント情報を抽出・フィルタリング ──────────────

class EventArticleFilter:

    def filter_and_extract(self, articles: list[dict],
                            category_name: str,
                            skip_kanto_filter: bool = False) -> list[dict]:
        today    = date_type.today()
        ref_year = today.year
        end_date = today + timedelta(days=DAYS_AHEAD)

        events     = []
        seen_titles = set()
        skipped    = {"past": 0, "no_date": 0, "out_of_kanto": 0}

        for article in articles:
            title = article["title"]
            desc  = article["description"]
            url   = article["url"]
            text  = title + " " + desc

            # ── イベントキーワード判定 ──
            if not any(kw in text for kw in EVENT_KEYWORDS):
                continue

            # ── タイトル重複除去 ──
            key = re.sub(r'\s+', '', title)[:30]
            if key in seen_titles:
                continue
            seen_titles.add(key)

            # ── 人事図書館イベントは無条件で通過 ──
            is_jinjitoshokan = any(kw in text for kw in JINJITOSHOKAN_KEYWORDS)

            # ── 日付抽出・パース ──
            raw_date    = ""
            parsed_date = None
            for pat, _ in DATE_PATTERNS:
                m = re.search(pat, text)
                if m:
                    raw_date    = m.group()
                    parsed_date = parse_japanese_date(raw_date, ref_year)
                    break

            # ── フィルタ①：日付不明は除外（人事図書館は除く） ──
            if not parsed_date and not is_jinjitoshokan:
                skipped["no_date"] += 1
                continue

            # ── フィルタ②：今日〜1ヶ月以内のみ（人事図書館は除く） ──
            if parsed_date and not is_jinjitoshokan:
                if parsed_date < today or parsed_date > end_date:
                    skipped["past"] += 1
                    continue

            # ── 開催形式を判定 ──
            text_lower = text.lower()
            if any(kw.lower() in text_lower for kw in ONLINE_KEYWORDS):
                fmt = "オンライン"
            else:
                fmt = "オフライン"  # 不明はオフライン扱いで関東チェック対象に

            # ── フィルタ③：オフラインの場合は関東圏のみ（skip_kanto_filter=True or 人事図書館は除く） ──
            if fmt == "オフライン" and not is_jinjitoshokan and not skip_kanto_filter:
                if not any(kw in text for kw in KANTO_KEYWORDS):
                    skipped["out_of_kanto"] += 1
                    continue

            # ── 参加費を判定 ──
            if any(kw in text for kw in ["無料", "参加費無料", "参加無料"]):
                fee = "無料"
            elif any(kw in text for kw in ["有料", "参加費", "円", "¥"]):
                fee = "有料"
            else:
                fee = "不明"

            # タイトルから媒体名を除去
            clean_title = re.sub(r'\s*[-–—]\s*[^\-–—]+$', '', title).strip()

            events.append({
                "title":           clean_title or title,
                "date":            raw_date,
                "parsed_date":     parsed_date,
                "format":          fmt,
                "summary":         desc[:200],
                "url":             url,
                "fee":             fee,
                "is_jinjitoshokan": is_jinjitoshokan,
            })

        # 開催日順にソート（人事図書館イベントは先頭に）
        events.sort(key=lambda e: (
            0 if e["is_jinjitoshokan"] else 1,
            e["parsed_date"] if e["parsed_date"] else date_type(9999, 12, 31)
        ))

        print(f"  EventArticleFilter（{category_name}）: "
              f"{len(articles)}件 → {len(events)}件採用"
              f"（除外：日付不明{skipped['no_date']}件、"
              f"過去日{skipped['past']}件、"
              f"関東圏外{skipped['out_of_kanto']}件）")
        return events


# ── X投稿URLから内容を取得 ────────────────────────────────

class XPostCollector:
    """
    設定ファイルに記載された特定のX投稿URLから
    投稿内容・イベント情報を直接取得する。
    X APIなしで動作するが、指定URLのみが対象。
    """

    def fetch(self, post_urls: list[str]) -> list[dict]:
        events = []
        for url in post_urls:
            try:
                r = requests.get(
                    url,
                    headers={"User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    )},
                    timeout=10
                )
                if r.status_code != 200:
                    print(f"  X投稿取得エラー: {r.status_code} ({url})")
                    continue

                # full_text を HTML から抽出
                m = re.search(r'"full_text":"((?:[^"\\]|\\.)*)\"', r.text)
                if not m:
                    print(f"  X投稿: full_text 抽出できず ({url})")
                    continue

                text = m.group(1).replace("\\n", "\n").replace('\\"', '"')

                # イベント関連かチェック
                if not any(kw in text for kw in EVENT_KEYWORDS):
                    print(f"  X投稿: イベントキーワードなし（スキップ）")
                    continue

                # アカウント名を取得
                handle_m = re.search(r'x\.com/([^/]+)/status/', url)
                account = f"@{handle_m.group(1)}" if handle_m else "X投稿"

                # 日付を抽出
                ref_year    = date_type.today().year
                raw_date    = ""
                parsed_date = None
                for pat, _ in DATE_PATTERNS:
                    dm = re.search(pat, text)
                    if dm:
                        raw_date    = dm.group()
                        parsed_date = parse_japanese_date(raw_date, ref_year)
                        break

                events.append({
                    "title":           text[:60].replace("\n", " ").strip() + "…",
                    "date":            raw_date,
                    "parsed_date":     parsed_date,
                    "format":          "オンライン" if any(k.lower() in text.lower()
                                        for k in ONLINE_KEYWORDS) else "不明",
                    "summary":         text[:300],
                    "url":             url,
                    "fee":             "無料" if "無料" in text else "不明",
                    "is_jinjitoshokan": False,
                    "source":          account,
                    "is_x_post":       True,
                })
                print(f"  X投稿取得: {account} - {text[:50]}...")
                time.sleep(0.5)

            except Exception as e:
                print(f"  X投稿取得エラー: {e}")

        return events


# ── カテゴリ担当エージェント ──────────────────────────────

class CategoryEventAgent:

    def __init__(self):
        self.collector = NewsEventCollector()
        self.extractor = EventArticleFilter()

    def collect(self, category: dict, max_per_keyword: int) -> dict:
        name              = category["name"]
        skip_kanto        = category.get("skip_kanto_filter", False)
        print(f"\n  [{name}] Google News RSSからイベント告知を収集中...")
        articles = self.collector.fetch(category["news_keywords"], max_per_keyword)
        print(f"  [{name}] RSS取得: {len(articles)}件")
        events = self.extractor.filter_and_extract(articles, name, skip_kanto)
        return {"category": category, "events": events}


# ── Notion に投稿（洗練UI版） ─────────────────────────────

class EventWriterAgent:

    MAX_PER_CAT = 12
    CHUNK_SIZE  = 90

    def post(self, notion: NotionClient, parent_page_id: str,
             results: list[dict], run_dt: datetime,
             title_prefix: str,
             x_events: list = None) -> dict:

        today   = date_type.today()
        end_dt  = today + timedelta(days=DAYS_AHEAD)

        # ── Notion ブロックヘルパー ──────────────────────

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

        def paragraph(text, url=None, bold=False, color=None):
            ann = {}
            if bold:
                ann["bold"] = True
            if color:
                ann["color"] = color
            rich = {"type": "text", "text": {"content": text},
                    "annotations": ann} if ann else \
                   {"type": "text", "text": {"content": text}}
            if url:
                rich["text"]["link"] = {"url": url}
            return {"object": "block", "type": "paragraph",
                    "paragraph": {"rich_text": [rich]}}

        def quote_block(text):
            return {"object": "block", "type": "quote",
                    "quote": {"rich_text": [{"type": "text",
                              "text": {"content": text[:1900]}}]}}

        def divider():
            return {"object": "block", "type": "divider", "divider": {}}

        def bulleted(text, url=None):
            rich = {"type": "text", "text": {"content": text}}
            if url:
                rich["text"]["link"] = {"url": url}
            return {
                "object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [rich]}
            }

        # ── ページ全体の組み立て ──────────────────────────

        # 各カテゴリを上限件数に絞る
        for r in results:
            r["events"] = r["events"][:self.MAX_PER_CAT]

        x_events  = x_events or []
        total     = sum(len(r["events"]) for r in results) + len(x_events)
        end_label = end_dt.strftime("%-m月%-d日")

        title = (f"{title_prefix} "
                 f"{run_dt.strftime('%Y/%m/%d')}")

        summary_parts = [
            f"{r['category']['emoji']} {r['category']['name']}："
            f"{len(r['events'])}件"
            for r in results
        ]

        blocks = []

        # ── ① サマリーバナー ──────────────────────────────
        blocks.append(callout(
            f"📅  {today.strftime('%-m月%-d日')} 〜 {end_label} の人事キャリア関連イベント\n\n"
            + "　｜　".join(summary_parts)
            + f"\n\n合計 {total}件　｜　今日以降1ヶ月分・関東圏のみ・日付不明除外",
            "🗓️", "blue_background"
        ))
        blocks.append(divider())

        # ── ② カテゴリ別セクション ────────────────────────
        for r in results:
            cat    = r["category"]
            events = r["events"]

            # カテゴリ見出し
            blocks.append(heading(
                f"{cat['emoji']} {cat['name']}  /{len(events)}件/", 2
            ))

            if not events:
                blocks.append(callout(
                    "今後1ヶ月以内の開催情報が見つかりませんでした。",
                    "📭", "gray_background"
                ))
                blocks.append(divider())
                continue

            for ev in events:
                pd   = ev["parsed_date"]
                d_until = days_until(pd) if pd else None

                # ── 日付ヘッダー（最も目立つ部分） ──
                if pd and d_until is not None:
                    if d_until == 0:
                        date_str = f"📅  {date_display(pd)}  ━━  🔴 本日開催！"
                        bg = "red_background"
                    elif d_until <= 7:
                        date_str = f"📅  {date_display(pd)}  ━━  あと {d_until}日 🔴"
                        bg = "red_background"
                    elif d_until <= 14:
                        date_str = f"📅  {date_display(pd)}  ━━  あと {d_until}日 🟠"
                        bg = "orange_background"
                    elif d_until <= 21:
                        date_str = f"📅  {date_display(pd)}  ━━  あと {d_until}日 🟡"
                        bg = "yellow_background"
                    else:
                        date_str = f"📅  {date_display(pd)}  ━━  あと {d_until}日"
                        bg = "green_background"
                else:
                    date_str = "📅  開催日不明"
                    bg = "gray_background"

                blocks.append(callout(date_str, "📅", bg))

                # 人事図書館バッジ
                if ev.get("is_jinjitoshokan"):
                    blocks.append(callout(
                        "📚 人事図書館イベント", "📚", "purple_background"
                    ))

                # タイトル
                blocks.append(heading(ev["title"], 3))

                # 詳細行（形式・参加費）
                detail_parts = []
                if ev["format"] == "オンライン":
                    detail_parts.append("💻 オンライン開催")
                elif ev["format"] == "オフライン":
                    detail_parts.append("📍 会場開催")
                else:
                    detail_parts.append("📌 形式不明")
                if ev["fee"] == "無料":
                    detail_parts.append("🆓 参加無料")
                elif ev["fee"] == "有料":
                    detail_parts.append("💰 有料")

                if detail_parts:
                    blocks.append(paragraph("　　".join(detail_parts)))

                # 概要
                if ev["summary"]:
                    blocks.append(quote_block(ev["summary"]))

                # リンク
                if ev["url"]:
                    blocks.append(paragraph(
                        "🔗  詳細・申し込みはこちら",
                        url=ev["url"], bold=True
                    ))

                # スペーサー（カード間の余白）
                blocks.append(paragraph(""))

            blocks.append(divider())

        # ── X投稿セクション ──────────────────────────────────
        if x_events:
            blocks.append(heading("𝕏 X（Twitter）投稿からのイベント情報", 2))
            blocks.append(callout(
                "設定ファイルに登録されたX投稿URLから取得したイベント情報です。\n"
                "config/settings.json の x_post_urls にURLを追加してください。",
                "𝕏", "gray_background"
            ))
            for ev in x_events:
                pd      = ev.get("parsed_date")
                d_until = days_until(pd) if pd else None

                if pd and d_until is not None:
                    if d_until <= 7:
                        date_str = f"📅  {date_display(pd)}  ━━  あと {d_until}日 🔴"
                        bg = "red_background"
                    elif d_until <= 14:
                        date_str = f"📅  {date_display(pd)}  ━━  あと {d_until}日 🟠"
                        bg = "orange_background"
                    elif d_until <= 21:
                        date_str = f"📅  {date_display(pd)}  ━━  あと {d_until}日 🟡"
                        bg = "yellow_background"
                    else:
                        date_str = f"📅  {date_display(pd)}  ━━  あと {d_until}日"
                        bg = "green_background"
                else:
                    date_str = "📅  開催日：投稿を確認"
                    bg = "gray_background"

                blocks.append(callout(date_str, "📅", bg))
                source = ev.get("source", "X投稿")
                blocks.append(heading(f"{source}  の投稿", 3))
                if ev.get("summary"):
                    blocks.append(quote_block(ev["summary"]))
                if ev.get("url"):
                    blocks.append(paragraph("🔗  元の投稿を見る", url=ev["url"], bold=True))
                blocks.append(paragraph(""))

            blocks.append(divider())

        # ── Notionページ作成（100ブロック制限に対応） ──────
        first_chunk = blocks[:self.CHUNK_SIZE]
        remaining   = blocks[self.CHUNK_SIZE:]

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

        for i in range(0, len(remaining), self.CHUNK_SIZE):
            notion.blocks.children.append(
                block_id=page_id,
                children=remaining[i:i + self.CHUNK_SIZE]
            )
            time.sleep(0.3)

        print(f"  ページ作成完了（{len(blocks)}ブロック）")

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

        date_short = run_dt.strftime("%-m/%-d")
        entry = {
            "object": "block", "type": "callout",
            "callout": {
                "rich_text": [
                    {"type": "text",
                     "text": {"content": f"🗓️ {date_short} 更新　計{total}件\n"},
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

        today   = date_type.today()
        end_dt  = today + timedelta(days=DAYS_AHEAD)
        print(f"=== 人事イベント収集開始: {now.strftime('%Y/%m/%d %H:%M')} ===")
        print(f"    対象期間: {today} 〜 {end_dt}")
        print(f"    対象カテゴリ: {len(categories)}個")

        agent   = CategoryEventAgent()
        results = []

        for cat in categories:
            result = agent.collect(cat, max_arts)
            results.append(result)

        # X投稿URLから追加収集
        x_urls = self.settings.get("x_post_urls", [])
        x_events = []
        if x_urls:
            print(f"\n  [X投稿] {len(x_urls)}件のURLから情報収集中...")
            x_collector = XPostCollector()
            x_events = x_collector.fetch(x_urls)
            print(f"  [X投稿] {len(x_events)}件のイベント投稿を取得")

        total = sum(len(r["events"]) for r in results) + len(x_events)
        print(f"\n合計 {total}件（今日〜1ヶ月以内）")

        print("\n[EventWriterAgent] Notionに投稿中...")
        writer = EventWriterAgent()
        info   = writer.post(
            self.notion, self.page_id, results, now,
            self.settings["notion"]["title_prefix"],
            x_events=x_events
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
