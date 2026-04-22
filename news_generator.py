#!/usr/bin/env python3
"""
可轉債新聞整理器
- 讀取 cb_screener.py 產生的 new_for_news.json（當日新上榜 CB 清單）
- 針對每檔新 CB 抓近期新聞（Google News + TWSE 重訊）
- 用 OpenAI GPT-4o 產生結構化摘要
- 輸出 HTML 頁面到 cb-dashboard/news/{code}.html
- 回傳新聞連結清單給 main，供 Telegram 推播使用

Usage:
    python3 news_generator.py                 # 正式執行
    python3 news_generator.py --dry-run       # 不呼叫 OpenAI（模擬）
    python3 news_generator.py --code 33245    # 只處理單檔
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree as ET

import requests


# ═══════════════════════════════════════════
#  設定
# ═══════════════════════════════════════════
NEW_FOR_NEWS_FILE = os.path.join("history", "new_for_news.json")
NEWS_OUTPUT_DIR = os.path.join("news_out")  # 相對於後端執行目錄（之後由 workflow 複製到前端 repo）
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = "gpt-4o"

# 每篇最多丟多少字給 GPT（控制成本）
MAX_NEWS_CHARS = 8000

# 新聞時間範圍（天）
NEWS_WINDOW_DAYS = 90

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}


# ═══════════════════════════════════════════
#  1. 取得股票代號（從 CB 代號推算）
# ═══════════════════════════════════════════
def cb_code_to_stock(cb_code: str) -> str:
    """
    CB 代號最後 1 碼通常是發行次序（如 1/2/3 代表第幾次發行）
    例：33245 → 3324、62822 → 6282、811210 → 8112
    規則：去掉最後一個數字字元；6 碼的特殊情況去掉最後 2 碼
    """
    cb_code = str(cb_code).strip()
    if len(cb_code) <= 4:
        return cb_code
    if len(cb_code) == 6:
        # 6 碼 CB 通常是去掉最後 2 碼（如 811210 → 8112）
        return cb_code[:4]
    return cb_code[:-1]


# ═══════════════════════════════════════════
#  2. Google News RSS
# ═══════════════════════════════════════════
def fetch_google_news(query: str, days: int = NEWS_WINDOW_DAYS, limit: int = 20) -> list:
    """從 Google News RSS 抓新聞，回傳 [{title, link, date, source}, ...]"""
    url = (
        f"https://news.google.com/rss/search?"
        f"q={quote(query)}+when:{days}d&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        print(f"  [警告] Google News 抓取失敗 ({query}): {e}")
        return []

    items = []
    for item in root.findall(".//item")[:limit]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        # source 名稱常在 <source> 或在 title 後面「 - XXX」
        source = ""
        src_el = item.find("source")
        if src_el is not None and src_el.text:
            source = src_el.text.strip()
        else:
            m = re.search(r" - ([^-]+)$", title)
            if m:
                source = m.group(1).strip()
                title = title[: m.start()].strip()

        # 解析日期
        try:
            date_obj = datetime.strptime(pub_date, "%a, %d %b %Y %H:%M:%S %Z")
            date_str = date_obj.strftime("%Y-%m-%d")
        except ValueError:
            date_str = pub_date[:16] if pub_date else ""

        if title and link:
            items.append({
                "title": title,
                "link": link,
                "date": date_str,
                "source": source,
            })
    return items


# ═══════════════════════════════════════════
#  3. TWSE 重大訊息公告
# ═══════════════════════════════════════════
def fetch_twse_material_info(stock_code: str, days: int = NEWS_WINDOW_DAYS) -> list:
    """從公開資訊觀測站抓重大訊息，回傳 [{title, date, ...}, ...]"""
    url = "https://mops.twse.com.tw/mops/web/ajax_t05st01"
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)

    data = {
        "encodeURIComponent": "1",
        "step": "1",
        "firstin": "1",
        "off": "1",
        "keyword4": "",
        "code1": "",
        "TYPEK2": "",
        "checkbtn": "",
        "queryName": "co_id",
        "inpuType": "co_id",
        "TYPEK": "all",
        "co_id": stock_code,
        "year": str(end_date.year - 1911),
        "month": f"{end_date.month:02d}",
        "b_date": start_date.strftime("%Y%m%d"),
        "e_date": end_date.strftime("%Y%m%d"),
    }
    try:
        resp = requests.post(url, data=data, headers=HEADERS, timeout=20)
        resp.encoding = "utf-8"
        if resp.status_code != 200:
            return []
        # 解析 HTML table
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", resp.text, re.DOTALL)
        items = []
        for row in rows:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
            if len(cells) >= 5:
                def clean(s):
                    s = re.sub(r"<[^>]+>", "", s)
                    return re.sub(r"\s+", " ", s).strip()
                date = clean(cells[0])
                title = clean(cells[4]) if len(cells) > 4 else ""
                if date and title and re.match(r"\d+/\d+/\d+", date):
                    items.append({"date": date, "title": title})
        return items[:20]
    except Exception as e:
        print(f"  [警告] TWSE 重訊抓取失敗 ({stock_code}): {e}")
        return []


# ═══════════════════════════════════════════
#  4. OpenAI 摘要生成
# ═══════════════════════════════════════════
def summarize_with_openai(
    api_key: str,
    company_name: str,
    stock_code: str,
    cb_code: str,
    cb_name: str,
    cb_info: dict,
    news_items: list,
    material_items: list,
) -> dict:
    """
    丟新聞標題給 GPT-4o，回傳結構化摘要
    回傳 {overall, financial, management, business, risks}
    """
    news_text = "\n".join(
        [f"- [{n['date']}] {n['title']}（{n['source']}）" for n in news_items]
    )[:MAX_NEWS_CHARS]

    material_text = "\n".join(
        [f"- [{m['date']}] {m['title']}" for m in material_items]
    )[:3000]

    cb_info_text = (
        f"可轉債代號 {cb_code} ({cb_name})\n"
        f"CB 市價 {cb_info.get('債券市價', '-')} · 溢價率 {cb_info.get('溢價率_pct', '-')}%\n"
        f"轉換價格 {cb_info.get('轉換價格', '-')} · 轉換價值 {cb_info.get('轉換價值', '-')}\n"
        f"到期日 {cb_info.get('到期日', '-')}"
    )

    system_prompt = """你是專業財經分析師，負責幫可轉債投資人整理公司近況。
要求：
1. 只根據提供的新聞和重訊，不要編造
2. 專注四個面向：財報/月營收表現、公司派動向（法說會、大股東動向）、業務與轉投資、可能風險
3. 每個面向 2-4 句話，白話中文
4. 若某面向沒有相關資料，直接寫「無近期相關訊息」
5. 語氣客觀，不做買賣建議
6. 輸出必須是有效的 JSON"""

    user_prompt = f"""請分析以下公司的近期狀況：

{cb_info_text}

=== 近期新聞標題（{len(news_items)} 則，近 {NEWS_WINDOW_DAYS} 天）===
{news_text or "（無資料）"}

=== TWSE 公開資訊觀測站重大訊息 ===
{material_text or "（無資料）"}

請以 JSON 格式輸出，結構如下（不要加 markdown code fence）：
{{
  "overall": "一段 2-3 句的整體概況描述",
  "financial": "財報與月營收面向的重點（2-4 句）",
  "management": "公司派動向（2-4 句）",
  "business": "業務發展與轉投資（2-4 句）",
  "risks": "可能風險提醒（1-3 句，無則寫「暫無明顯風險訊號」）"
}}"""

    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 1500,
    }

    try:
        resp = requests.post(
            OPENAI_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

        # 移除可能的 markdown code fence
        content = re.sub(r"^```(?:json)?\s*", "", content.strip())
        content = re.sub(r"\s*```$", "", content)

        return json.loads(content)
    except Exception as e:
        print(f"  [錯誤] OpenAI 呼叫失敗: {e}")
        return {
            "overall": f"（摘要生成失敗：{e}）",
            "financial": "無法取得",
            "management": "無法取得",
            "business": "無法取得",
            "risks": "無法取得",
        }


# ═══════════════════════════════════════════
#  5. HTML 頁面生成
# ═══════════════════════════════════════════
def render_html(
    cb_code: str,
    cb_name: str,
    stock_code: str,
    company_name: str,
    cb_info: dict,
    summary: dict,
    news_items: list,
    material_items: list,
    generated_at: str,
) -> str:
    """產生單檔新聞頁面 HTML"""
    # 篩掉重複連結
    seen_links = set()
    unique_news = []
    for n in news_items:
        if n["link"] not in seen_links:
            seen_links.add(n["link"])
            unique_news.append(n)
    news_items = unique_news[:15]  # 最多顯示 15 則

    news_html = ""
    for n in news_items:
        title = n["title"].replace("<", "&lt;").replace(">", "&gt;")
        source = (n.get("source") or "").replace("<", "&lt;").replace(">", "&gt;")
        date = n.get("date", "")
        news_html += f'''
        <li>
          <a href="{n['link']}" target="_blank" rel="noopener">{title}</a>
          <div class="meta">{date} · {source}</div>
        </li>'''

    material_html = ""
    if material_items:
        material_html = "<h3>📢 公開資訊觀測站重訊</h3><ul class='material-list'>"
        for m in material_items[:10]:
            title = m["title"].replace("<", "&lt;").replace(">", "&gt;")
            material_html += f'<li><span class="date">{m["date"]}</span> {title}</li>'
        material_html += "</ul>"

    def esc(s):
        return str(s).replace("<", "&lt;").replace(">", "&gt;") if s else "—"

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{cb_code} {cb_name} — 新聞整理</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, 'Noto Sans TC', sans-serif;
    background: #0a0e17; color: #e2e8f0;
    line-height: 1.7; padding: 24px 16px 48px;
    max-width: 760px; margin: 0 auto;
  }}
  .back-link {{ color: #22d3ee; text-decoration: none; font-size: 13px; margin-bottom: 12px; display: inline-block; }}
  .back-link:hover {{ text-decoration: underline; }}
  header {{ border-bottom: 1px solid #1e293b; padding-bottom: 16px; margin-bottom: 24px; }}
  h1 {{ font-size: 22px; color: #e2e8f0; font-weight: 600; }}
  h1 .cb-code {{ color: #22d3ee; font-family: 'SF Mono', monospace; }}
  h1 .sub {{ color: #64748b; font-size: 14px; font-weight: 400; margin-left: 8px; }}
  .meta-line {{ font-size: 12px; color: #64748b; margin-top: 4px; }}
  .cb-info {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
    gap: 8px 16px; background: rgba(15,23,42,0.6); padding: 12px 16px;
    border: 1px solid rgba(30,41,59,0.7); border-radius: 8px;
    margin-bottom: 24px; font-size: 13px;
  }}
  .cb-info div .k {{ color: #64748b; font-size: 11px; }}
  .cb-info div .v {{ color: #e2e8f0; font-weight: 500; font-variant-numeric: tabular-nums; }}
  section {{ margin-bottom: 28px; }}
  h2 {{
    font-size: 14px; color: #22d3ee; margin-bottom: 10px;
    letter-spacing: 1px; font-weight: 600;
  }}
  h3 {{ font-size: 14px; color: #94a3b8; margin: 24px 0 10px; font-weight: 500; }}
  .summary-block {{
    background: rgba(15,23,42,0.5); border-left: 3px solid #22d3ee;
    padding: 12px 16px; border-radius: 0 8px 8px 0; margin-bottom: 12px;
    font-size: 14px; color: #cbd5e1;
  }}
  .summary-block.risk {{ border-left-color: #fb7185; }}
  .news-list, .material-list {{ list-style: none; padding: 0; }}
  .news-list li {{ padding: 10px 0; border-bottom: 1px solid rgba(30,41,59,0.5); }}
  .news-list li:last-child {{ border-bottom: none; }}
  .news-list a {{ color: #e2e8f0; text-decoration: none; font-size: 14px; }}
  .news-list a:hover {{ color: #22d3ee; text-decoration: underline; }}
  .news-list .meta {{ font-size: 11px; color: #64748b; margin-top: 4px; }}
  .material-list li {{
    padding: 6px 0; font-size: 13px; color: #94a3b8; border-bottom: 1px dashed rgba(30,41,59,0.4);
  }}
  .material-list .date {{ color: #22d3ee; font-family: 'SF Mono', monospace; font-size: 11px; margin-right: 8px; }}
  footer {{ margin-top: 40px; padding-top: 20px; border-top: 1px solid #1e293b;
           text-align: center; font-size: 11px; color: #475569; }}
  .disclaimer {{
    background: rgba(244,63,94,0.05); border: 1px solid rgba(244,63,94,0.2);
    padding: 10px 14px; border-radius: 6px; font-size: 12px; color: #94a3b8;
    margin-top: 24px;
  }}
</style>
</head>
<body>
<a href="../index.html" class="back-link">← 回篩選器</a>

<header>
  <h1><span class="cb-code">{cb_code}</span> {esc(cb_name)}<span class="sub">· 股票代號 {stock_code} {esc(company_name)}</span></h1>
  <div class="meta-line">新聞整理生成於 {generated_at}</div>
</header>

<div class="cb-info">
  <div><div class="k">CB 市價</div><div class="v">{cb_info.get('債券市價', '—')}</div></div>
  <div><div class="k">溢價率</div><div class="v">{cb_info.get('溢價率_pct', '—')}%</div></div>
  <div><div class="k">標的股價</div><div class="v">{cb_info.get('標的股價', '—')}</div></div>
  <div><div class="k">轉換價</div><div class="v">{cb_info.get('轉換價格', '—')}</div></div>
  <div><div class="k">轉換價值</div><div class="v">{cb_info.get('轉換價值', '—')}</div></div>
  <div><div class="k">到期日</div><div class="v">{cb_info.get('到期日', '—')}</div></div>
</div>

<section>
  <h2>🔍 整體概況</h2>
  <div class="summary-block">{esc(summary.get('overall', '—'))}</div>
</section>

<section>
  <h2>💰 財報 / 月營收</h2>
  <div class="summary-block">{esc(summary.get('financial', '—'))}</div>
</section>

<section>
  <h2>👥 公司派動向</h2>
  <div class="summary-block">{esc(summary.get('management', '—'))}</div>
</section>

<section>
  <h2>🏢 業務 / 轉投資</h2>
  <div class="summary-block">{esc(summary.get('business', '—'))}</div>
</section>

<section>
  <h2>⚠️ 風險提醒</h2>
  <div class="summary-block risk">{esc(summary.get('risks', '—'))}</div>
</section>

<section>
  <h2>📰 新聞來源（共 {len(news_items)} 則）</h2>
  <ul class="news-list">{news_html or '<li>近期無相關新聞</li>'}</ul>
  {material_html}
</section>

<div class="disclaimer">
  本頁摘要由 AI 依公開新聞生成，僅供研究參考，不構成投資建議。實際投資決策請參考原始資料並自行判斷。
</div>

<footer>
  資料來源：Google News · 公開資訊觀測站 · OpenAI {OPENAI_MODEL} 摘要<br>
  生成時間：{generated_at}
</footer>
</body>
</html>
"""
    return html


# ═══════════════════════════════════════════
#  6. 主流程
# ═══════════════════════════════════════════
def load_new_codes() -> list:
    """讀取 cb_screener.py 寫入的當日新增 CB 清單"""
    if not os.path.exists(NEW_FOR_NEWS_FILE):
        print(f"[資訊] 找不到 {NEW_FOR_NEWS_FILE}，無新 CB 需要處理")
        return []
    with open(NEW_FOR_NEWS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("items", [])


def process_cb(item: dict, api_key: str, dry_run: bool = False) -> dict:
    """處理單檔 CB，回傳 {url_path, title}"""
    cb_code = str(item["代號"])
    cb_name = item.get("債券名稱", "")
    stock_code = str(item.get("股票代號") or cb_code_to_stock(cb_code))

    # 公司名（從 CB 名稱去掉末字如「三」「二」等）
    company_name = re.sub(r"[一二三四五六七八九十]+$", "", cb_name).strip()
    if not company_name:
        company_name = cb_name

    cb_info = {
        "債券市價": item.get("債券市價"),
        "溢價率_pct": round(item.get("溢價率", 0) * 100, 2) if item.get("溢價率") is not None else "—",
        "標的股價": item.get("標的股價"),
        "轉換價格": item.get("轉換價格"),
        "轉換價值": item.get("轉換價值"),
        "到期日": item.get("到期日"),
    }

    print(f"\n[處理] {cb_code} {cb_name} (股票 {stock_code} {company_name})")

    # 抓新聞（公司名 + 股票代號，兩個 query 合併去重）
    news = fetch_google_news(f"{company_name} {stock_code}")
    news += fetch_google_news(f"{company_name} 法說會")
    # 去重
    seen = set()
    unique_news = []
    for n in news:
        if n["link"] not in seen:
            seen.add(n["link"])
            unique_news.append(n)
    news = unique_news
    print(f"  [新聞] Google News 抓到 {len(news)} 則")

    # TWSE 重訊
    material = fetch_twse_material_info(stock_code)
    print(f"  [重訊] TWSE 抓到 {len(material)} 則")

    # OpenAI 摘要
    if dry_run:
        print("  [測試模式] 跳過 OpenAI 呼叫，使用 mock 摘要")
        summary = {
            "overall": f"[DRY-RUN] {company_name} ({stock_code}) 近期綜合概況 mock。",
            "financial": "[DRY-RUN] 財報 mock。",
            "management": "[DRY-RUN] 公司派 mock。",
            "business": "[DRY-RUN] 業務 mock。",
            "risks": "[DRY-RUN] 風險 mock。",
        }
    else:
        print(f"  [LLM] 呼叫 OpenAI {OPENAI_MODEL}...")
        summary = summarize_with_openai(
            api_key, company_name, stock_code, cb_code, cb_name,
            cb_info, news, material,
        )

    # 產生 HTML
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = render_html(
        cb_code, cb_name, stock_code, company_name, cb_info,
        summary, news, material, generated_at,
    )

    # 輸出
    os.makedirs(NEWS_OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(NEWS_OUTPUT_DIR, f"{cb_code}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  [輸出] {out_path}")

    return {
        "code": cb_code,
        "name": cb_name,
        "url_path": f"news/{cb_code}.html",  # 相對於 cb-dashboard/
        "title": f"{cb_code} {cb_name}",
    }


def send_telegram_summary(results: list, bot_token: str, chat_id: str, base_url: str):
    """新聞生成完後，發一則彙總 Telegram"""
    if not results:
        return
    if not base_url.endswith("/"):
        base_url += "/"

    lines = [
        "📰 *今日新上榜 CB — 新聞整理*",
        f"📅 {datetime.now().strftime('%Y/%m/%d')}",
        "─" * 24,
    ]
    for r in results:
        url = base_url + r["url_path"]
        lines.append(f"🔗 [{r['title']}]({url})")
    lines.append("")
    lines.append("_點連結查看公司基本面 + 近期新聞摘要_")
    msg = "\n".join(lines)

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        resp = requests.post(url, json={
            "chat_id": chat_id, "text": msg,
            "parse_mode": "Markdown", "disable_web_page_preview": True,
        }, timeout=10)
        if resp.status_code == 200:
            print(f"  [通知] Telegram 新聞彙總已發送（{len(results)} 檔）")
        else:
            print(f"  [錯誤] Telegram 失敗: {resp.text}")
    except Exception as e:
        print(f"  [錯誤] Telegram 連線失敗: {e}")


def main():
    parser = argparse.ArgumentParser(description="可轉債新聞整理器")
    parser.add_argument("--dry-run", action="store_true", help="不呼叫 OpenAI")
    parser.add_argument("--code", default=None, help="只處理特定 CB 代號")
    parser.add_argument("--api-key", default=None, help="OpenAI API key（預設讀環境變數）")
    parser.add_argument("--no-telegram", action="store_true", help="不發 Telegram")
    parser.add_argument("--dashboard-url", default="https://dale199707.github.io/cb-dashboard/",
                        help="Dashboard 網址，用於組新聞連結")
    args = parser.parse_args()

    print("=" * 44)
    print("  可轉債新聞整理器")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 44)

    # API key
    api_key = args.api_key or os.environ.get("OPENAI_API_KEY", "")
    if not api_key and not args.dry_run:
        print("[錯誤] 找不到 OPENAI_API_KEY，請設定環境變數或用 --dry-run")
        sys.exit(1)

    # 載入要處理的清單
    if args.code:
        # 單檔測試：用 latest.json 的資料
        latest_path = os.path.join("history", "latest.json")
        if not os.path.exists(latest_path):
            print(f"[錯誤] 找不到 {latest_path}")
            sys.exit(1)
        with open(latest_path, "r", encoding="utf-8") as f:
            latest = json.load(f)
        item = None
        for strategy_data in latest.get("strategies", {}).values():
            for r in strategy_data.get("data", []):
                if str(r.get("代號")) == args.code:
                    item = r
                    break
            if item:
                break
        if not item:
            print(f"[錯誤] latest.json 中找不到代號 {args.code}")
            sys.exit(1)
        items = [item]
    else:
        items = load_new_codes()

    if not items:
        print("[完成] 沒有新 CB 需要處理")
        return

    print(f"[處理] 共 {len(items)} 檔 CB 待整理")

    results = []
    for i, item in enumerate(items):
        try:
            result = process_cb(item, api_key, dry_run=args.dry_run)
            results.append(result)
            if i < len(items) - 1:
                time.sleep(2)  # 避免打爆 API
        except Exception as e:
            print(f"  [錯誤] 處理 {item.get('代號')} 失敗: {e}")

    # 儲存本次結果
    out_summary = os.path.join("history", "news_generated.json")
    with open(out_summary, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "results": results,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n[完成] 共產生 {len(results)} 個新聞頁，清單已存 {out_summary}")

    # 發 Telegram 彙總
    if not args.no_telegram and not args.dry_run and results:
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        if tg_token and tg_chat:
            send_telegram_summary(results, tg_token, tg_chat, args.dashboard_url)
        else:
            print("[警告] 沒有 TELEGRAM_BOT_TOKEN/CHAT_ID，跳過 Telegram 推播")


if __name__ == "__main__":
    main()
