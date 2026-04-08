#!/usr/bin/env python3
"""
可轉債盤後篩選系統 v2
- 讀取券商 .xls 可轉債清單（靜態資料：轉換價格、到期日、賣回條件等）
- 透過 TWSE/TPEX API 抓取標的股票當日收盤價
- 用當日股價重新計算轉換價值、溢折價
- 支援多組篩選策略（保守型、積極型、自訂）
- 透過 Telegram Bot 推播結果

Usage:
    python3 cb_screener.py                  # 使用預設 config.yaml
    python3 cb_screener.py -c myconfig.yaml # 指定設定檔
    python3 cb_screener.py --dry-run        # 測試模式（不發送 Telegram）
    python3 cb_screener.py --strategy 保守型  # 只跑特定策略
"""

import argparse
import glob
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import yaml


# ═══════════════════════════════════════════
#  1. 設定檔
# ═══════════════════════════════════════════
def load_config(config_path: str = "config.yaml") -> dict:
    path = Path(config_path)
    if not path.exists():
        print(f"[錯誤] 找不到設定檔: {config_path}")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ═══════════════════════════════════════════
#  2. 讀取 .xls 清單
# ═══════════════════════════════════════════
def find_latest_xls(folder: str, prefix: str) -> str:
    pattern_xls = os.path.join(folder, f"{prefix}*.xls")
    pattern_xlsx = os.path.join(folder, f"{prefix}*.xlsx")
    files = list(set(glob.glob(pattern_xls) + glob.glob(pattern_xlsx)))
    if not files:
        print(f"[錯誤] 在 {folder} 中找不到 {prefix}*.xls(x) 檔案")
        sys.exit(1)
    files_sorted = sorted(files, key=lambda f: (
        os.path.basename(f).rsplit(".", 1)[0],
        1 if f.endswith(".xlsx") else 0
    ))
    latest = files_sorted[-1]
    print(f"[資料] 使用檔案: {os.path.basename(latest)}")
    return latest


def read_cb_list(filepath: str) -> pd.DataFrame:
    """讀取元大證券可轉債清單"""
    ext = Path(filepath).suffix.lower()
    if ext == ".xls":
        try:
            df_raw = pd.read_excel(filepath, engine="xlrd", header=None, skiprows=4)
        except ImportError:
            print("[錯誤] 需要安裝 xlrd: pip install xlrd")
            sys.exit(1)
    else:
        df_raw = pd.read_excel(filepath, header=None, skiprows=4)

    expected_cols = [
        "代號", "債券名稱", "發行日", "到期日", "發行量億", "流通張數",
        "票面利率", "擔保情形", "轉換起始日", "轉換價格", "轉換比率",
        "債券市價", "標的股價_xls", "成交張數", "轉換價值_xls", "溢折價_xls",
        "下一賣回日", "下一賣回價", "賣回收益率"
    ]
    if len(df_raw.columns) == len(expected_cols):
        df_raw.columns = expected_cols
    else:
        df_raw = df_raw.iloc[:, :len(expected_cols)]
        df_raw.columns = expected_cols

    df = df_raw[df_raw["代號"].astype(str).str.match(r"^\d", na=False)].copy()

    numeric_cols = [
        "發行量億", "流通張數", "票面利率", "轉換價格", "轉換比率",
        "債券市價", "標的股價_xls", "成交張數", "轉換價值_xls", "溢折價_xls",
        "下一賣回價", "賣回收益率"
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    for col in ["發行日", "到期日", "轉換起始日", "下一賣回日"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    today = datetime.now()
    df["到期剩餘天數"] = (df["到期日"] - today).dt.days
    df["賣回剩餘天數"] = (df["下一賣回日"] - today).dt.days
    df["股票代號"] = df["代號"].astype(str).str[:4]
    df["有擔保"] = df["擔保情形"].apply(
        lambda x: "有" if str(x).startswith("有") else "無"
    )

    df = df.reset_index(drop=True)
    print(f"[資料] 共讀取 {len(df)} 檔可轉債")
    return df


# ═══════════════════════════════════════════
#  3. 抓取即時報價（TWSE/TPEX API）
#     同一支 API 可查股票和可轉債
# ═══════════════════════════════════════════
def _fetch_prices_from_twse(codes: list, label: str = "") -> dict:
    """透過 TWSE 即時報價 API 抓取收盤價（股票或可轉債皆可）"""
    prices = {}
    unique_codes = list(set(codes))
    if label:
        print(f"[報價] 準備抓取 {len(unique_codes)} 檔{label}...")

    batch_size = 20
    for i in range(0, len(unique_codes), batch_size):
        batch = unique_codes[i : i + batch_size]

        # 同時嘗試 tse（上市）和 otc（上櫃）
        query_parts = []
        for code in batch:
            query_parts.append(f"tse_{code}.tw")
            query_parts.append(f"otc_{code}.tw")

        query_str = "|".join(query_parts)
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={query_str}"

        try:
            resp = requests.get(url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            })
            if resp.status_code == 200:
                data = resp.json()
                for item in data.get("msgArray", []):
                    code = item.get("c", "")
                    close_price = item.get("z", "")
                    yesterday = item.get("y", "")

                    if close_price and close_price != "-":
                        try:
                            prices[code] = float(close_price)
                        except ValueError:
                            pass
                    elif yesterday and yesterday != "-":
                        try:
                            prices[code] = float(yesterday)
                        except ValueError:
                            pass
        except requests.exceptions.RequestException as e:
            print(f"[警告] API 請求失敗 (batch {i//batch_size + 1}): {e}")

        if i + batch_size < len(unique_codes):
            time.sleep(3)

    found = len(prices)
    total = len(unique_codes)
    if label:
        print(f"[報價] {label}: 成功 {found}/{total} 檔")

    return prices


def load_prefetch(output_dir: str) -> dict:
    """讀取 cb_prefetch.py 存好的盤中可轉債報價"""
    path = os.path.join(output_dir, "cb_prefetch.json")
    if not os.path.exists(path):
        print(f"[報價] 找不到 prefetch 檔案: {path}")
        return {}

    with open(path, "r", encoding="utf-8") as f:
        prefetch = json.load(f)

    fetch_date = prefetch.get("fetch_date", "")
    today = datetime.now().strftime("%Y%m%d")

    if fetch_date != today:
        print(f"[報價] prefetch 是 {fetch_date} 的資料（今天 {today}），跳過")
        return {}

    fetch_time = prefetch.get("fetch_time", "")
    count = prefetch.get("count", 0)
    print(f"[報價] 讀取 prefetch: {fetch_time}，共 {count} 檔可轉債")

    return prefetch.get("data", {})


def load_volume_history(output_dir: str, days: int = 5) -> dict:
    """
    讀取近 N 天的歷史成交量，回傳每檔 CB 的平均成交量
    回傳 {代號: 平均成交量}
    """
    history_dir = os.path.join(output_dir, "cb_history")
    if not os.path.exists(history_dir):
        return {}

    # 找所有歷史檔案，按日期排序取最近 N 天
    files = sorted(glob.glob(os.path.join(history_dir, "*.json")), reverse=True)
    files = files[:days]

    if not files:
        return {}

    print(f"[歷史] 讀取近 {len(files)} 天成交量資料")

    # 累積每檔 CB 的成交量
    vol_records = {}  # {代號: [vol_day1, vol_day2, ...]}
    for fpath in files:
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            for code, info in data.get("data", {}).items():
                vol = info.get("volume", 0)
                if code not in vol_records:
                    vol_records[code] = []
                vol_records[code].append(vol)
        except (json.JSONDecodeError, KeyError):
            continue

    # 算平均
    avg_volumes = {}
    for code, vols in vol_records.items():
        avg_volumes[code] = round(sum(vols) / len(vols))

    print(f"[歷史] 計算 {len(avg_volumes)} 檔 CB 的 {len(files)} 日平均成交量")
    return avg_volumes


def fetch_all_live_prices(df: pd.DataFrame, prefetch_dir: str = "") -> tuple:
    """
    抓取標的股票（盤後 API）+ 可轉債（優先用 prefetch 盤中資料）
    回傳 (stock_prices, cb_prices, cb_volumes, avg_volumes)
    """
    # 1) 標的股票（盤後 API 有值）
    stock_codes = df["股票代號"].unique().tolist()
    stock_prices = _fetch_prices_from_twse(stock_codes, "標的股票")

    # 2) 可轉債：優先用 prefetch
    cb_prices = {}
    cb_volumes = {}
    prefetch_data = {}

    if prefetch_dir:
        prefetch_data = load_prefetch(prefetch_dir)

    if prefetch_data:
        for code, info in prefetch_data.items():
            if info.get("source") == "盤中":
                cb_prices[code] = info["price"]
                cb_volumes[code] = info.get("volume", 0)
        print(f"[報價] 可轉債: prefetch 取得 {len(cb_prices)} 檔盤中價")
    else:
        # fallback：直接用 API（盤中執行時可用）
        print("[報價] 無 prefetch，嘗試即時抓取可轉債報價...")
        time.sleep(3)
        cb_codes = df["代號"].astype(str).unique().tolist()
        cb_prices = _fetch_prices_from_twse(cb_codes, "可轉債")

    # 3) 歷史平均成交量
    avg_volumes = {}
    if prefetch_dir:
        avg_volumes = load_volume_history(prefetch_dir, days=5)

    return stock_prices, cb_prices, cb_volumes, avg_volumes


# ═══════════════════════════════════════════
#  4. 重新計算轉換價值與溢折價
# ═══════════════════════════════════════════
def recalculate_with_live_prices(
    df: pd.DataFrame, stock_prices: dict, cb_prices: dict,
    cb_volumes: dict = None, avg_volumes: dict = None
) -> pd.DataFrame:
    """用即時股價和即時債券價重新計算"""
    df = df.copy()
    if cb_volumes is None:
        cb_volumes = {}
    if avg_volumes is None:
        avg_volumes = {}

    # 標的股價
    df["標的股價_即時"] = df["股票代號"].map(stock_prices)
    df["標的股價"] = df["標的股價_即時"].fillna(df["標的股價_xls"])
    df["股價來源"] = df["標的股價_即時"].apply(
        lambda x: "即時" if pd.notna(x) else "清單"
    )

    # 可轉債市價
    df["債券市價_即時"] = df["代號"].astype(str).map(cb_prices)
    df["債券市價_原"] = df["債券市價"]
    mask = df["債券市價_即時"].notna()
    df.loc[mask, "債券市價"] = df.loc[mask, "債券市價_即時"]
    df["債券價來源"] = mask.map({True: "即時", False: "清單"})

    # 可轉債成交量（當日）
    if cb_volumes:
        df["CB成交量"] = df["代號"].astype(str).map(cb_volumes).fillna(0).astype(int)
    else:
        df["CB成交量"] = df["成交張數"].fillna(0).astype(int)

    # 可轉債近 N 日平均成交量
    if avg_volumes:
        df["CB均量"] = df["代號"].astype(str).map(avg_volumes).fillna(0).astype(int)
    else:
        df["CB均量"] = df["CB成交量"]  # 沒有歷史就用當日

    # 重新計算
    df["轉換價值"] = (100 / df["轉換價格"]) * df["標的股價"]
    df["溢折價"] = (df["債券市價"] - df["轉換價值"]) / df["轉換價值"]

    # 額外衍生欄位
    df["溢價率"] = (df["債券市價"] / df["轉換價值"]) - 1
    df["股價轉換價比"] = (df["標的股價"] / df["轉換價格"]) - 1

    stock_live = (df["股價來源"] == "即時").sum()
    cb_live = (df["債券價來源"] == "即時").sum()
    print(f"[計算] 股價即時 {stock_live} 檔 ｜ 債券價即時 {cb_live} 檔")

    return df


# ═══════════════════════════════════════════
#  5. 篩選策略（支援 filters + custom_filter）
# ═══════════════════════════════════════════

# --- 自訂策略函式 ---
CUSTOM_FILTERS = {}


def register_filter(name):
    """裝飾器：註冊自訂篩選函式"""
    def decorator(func):
        CUSTOM_FILTERS[name] = func
        return func
    return decorator


@register_filter("可轉債資優生")
def filter_cb_honor(df):
    """
    CB市價 103~160
    且（股價在轉換價 -20%~+30% 或 CB市價 > 轉換價值）
    """
    # 基本條件：CB市價 103~160
    mask_price = (df["債券市價"] > 103) & (df["債券市價"] < 160)

    # OR 條件 1：股價在轉換價格的 -20% ~ +30%
    mask_stock = (df["股價轉換價比"] >= -0.20) & (df["股價轉換價比"] <= 0.30)

    # OR 條件 2：CB市價 > 轉換價值
    mask_cv = df["債券市價"] > df["轉換價值"]

    result = df[mask_price & (mask_stock | mask_cv)]
    return result


@register_filter("突破轉換價")
def filter_breakthrough(df):
    """
    CB收盤價 > 轉換價值 且溢價率 < 5%
    股價 > 轉換價格 0%~10%
    CB日成交量 > 200 張
    """
    # 條件 1：CB > 轉換價值，且溢價率 < 5%
    mask_premium = (df["債券市價"] > df["轉換價值"]) & (df["溢價率"] < 0.05)

    # 條件 2：股價高於轉換價格 0%~10%
    mask_stock = (df["股價轉換價比"] > 0) & (df["股價轉換價比"] < 0.10)

    # 條件 3：CB 成交量 > 200
    mask_vol = df["CB成交量"] > 200

    result = df[mask_premium & mask_stock & mask_vol]
    return result


def apply_strategy(df: pd.DataFrame, strategy: dict, strategy_name: str = "") -> pd.DataFrame:
    result = df.copy()

    # 如果有對應的自訂篩選函式，優先使用
    if strategy_name in CUSTOM_FILTERS:
        before = len(result)
        result = CUSTOM_FILTERS[strategy_name](result)
        after = len(result)
        print(f"    自訂篩選: {before} → {after}")
    else:
        # 標準 min/max 篩選
        filters = strategy.get("filters", {})
        applied = []

        for col_name, conditions in filters.items():
            if col_name not in result.columns:
                print(f"  [警告] 欄位 '{col_name}' 不存在，跳過")
                continue
            before = len(result)
            if "min" in conditions and conditions["min"] is not None:
                result = result[result[col_name] >= conditions["min"]]
            if "max" in conditions and conditions["max"] is not None:
                result = result[result[col_name] <= conditions["max"]]
            after = len(result)
            applied.append(f"    {col_name}: {before} → {after}")

        if applied:
            for a in applied:
                print(a)

    sort_config = strategy.get("sort", {"by": "溢折價", "ascending": True})
    col = sort_config.get("by", "溢折價")
    asc = sort_config.get("ascending", True)
    if col in result.columns:
        result = result.sort_values(by=col, ascending=asc, na_position="last")

    return result


# ═══════════════════════════════════════════
#  6. 格式化 Telegram 訊息
# ═══════════════════════════════════════════
def format_telegram_message(
    strategy_name, strategy_desc, df, max_results=15, xls_date=""
):
    today_str = datetime.now().strftime("%Y/%m/%d")
    lines = []
    lines.append(f"📊 *{strategy_name}*")
    if strategy_desc:
        lines.append(f"_{strategy_desc}_")
    lines.append(f"📅 {today_str}（篩出 {len(df)} 檔）")
    if xls_date:
        lines.append(f"📋 清單: {xls_date}")
    lines.append("─" * 24)

    for _, row in df.head(max_results).iterrows():
        code = str(row.get("代號", ""))
        name = str(row.get("債券名稱", ""))
        bond_price = row.get("債券市價", 0)
        premium_rate = row.get("溢價率", 0)
        cb_vol = row.get("CB成交量", 0)
        cb_avg = row.get("CB均量", 0)
        days_left = row.get("到期剩餘天數", 0)
        cv = row.get("轉換價值", 0)
        stock_price = row.get("標的股價", 0)
        conv_price = row.get("轉換價格", 0)
        stock_src = row.get("股價來源", "清單")
        bond_src = row.get("債券價來源", "清單")
        guaranteed = row.get("有擔保", "")

        emoji = "🟢" if premium_rate < 0 else ("🟡" if premium_rate < 0.05 else "🔴")
        s_mark = "⚡" if stock_src == "即時" else "📋"
        b_mark = "⚡" if bond_src == "即時" else "📋"
        shield = "🛡" if guaranteed == "有" else ""

        lines.append(f"{emoji} *{code} {name}* {shield}")
        lines.append(f"  {b_mark}CB {bond_price:.2f} ｜溢價率 {premium_rate:+.1%}")
        lines.append(f"  {s_mark}股價 {stock_price:.2f} ｜轉換價 {conv_price:.2f}")
        lines.append(f"  轉換價值 {cv:.2f} ｜成交 {int(cb_vol)} 張 ｜均量 {int(cb_avg)}")
        lines.append("")

    if len(df) > max_results:
        lines.append(f"⋯ 還有 {len(df) - max_results} 檔未顯示")
    lines.append("")
    lines.append("🟢折價 🟡微溢價 🔴溢價 ⚡即時報價 📋清單價 🛡擔保")

    return "\n".join(lines)


# ═══════════════════════════════════════════
#  7. Telegram
# ═══════════════════════════════════════════
def send_telegram(bot_token, chat_id, message):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    max_len = 4000
    parts = [message] if len(message) <= max_len else []
    if not parts:
        current = ""
        for line in message.split("\n"):
            if len(current) + len(line) + 1 > max_len:
                parts.append(current)
                current = line
            else:
                current += "\n" + line if current else line
        if current:
            parts.append(current)

    for i, part in enumerate(parts):
        try:
            resp = requests.post(url, json={
                "chat_id": chat_id, "text": part,
                "parse_mode": "Markdown", "disable_web_page_preview": True,
            }, timeout=10)
            if resp.status_code == 200:
                print(f"  [通知] Telegram 已發送 ({i+1}/{len(parts)})")
            else:
                print(f"  [錯誤] Telegram 失敗: {resp.text}")
        except requests.exceptions.RequestException as e:
            print(f"  [錯誤] Telegram 連線失敗: {e}")


# ═══════════════════════════════════════════
#  8. 主程式
# ═══════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="可轉債盤後篩選系統 v2")
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="不發送 Telegram")
    parser.add_argument("--strategy", default=None, help="只跑特定策略")
    parser.add_argument("--no-live", action="store_true", help="不抓即時股價")
    args = parser.parse_args()

    print("=" * 44)
    print("  可轉債盤後篩選系統 v2")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 44)

    config = load_config(args.config)

    # 讀取 .xls
    xls_path = find_latest_xls(
        config["data"]["xls_folder"], config["data"]["file_prefix"]
    )
    date_match = re.search(r"(\d{8})", os.path.basename(xls_path))
    xls_date = f"{date_match.group(1)[:4]}/{date_match.group(1)[4:6]}/{date_match.group(1)[6:8]}" if date_match else ""

    df = read_cb_list(xls_path)

    output_config = config.get("output", {})

    # 抓即時報價（股票 + 可轉債）
    if not args.no_live:
        prefetch_dir = output_config.get("csv_folder", "./output")
        stock_prices, cb_prices, cb_volumes, avg_volumes = fetch_all_live_prices(df, prefetch_dir)
        df = recalculate_with_live_prices(df, stock_prices, cb_prices, cb_volumes, avg_volumes)
    else:
        print("[報價] 跳過即時報價（使用清單價格）")
        df["標的股價"] = df["標的股價_xls"]
        df["轉換價值"] = df["轉換價值_xls"]
        df["溢折價"] = df["溢折價_xls"]
        df["股價來源"] = "清單"
        df["債券價來源"] = "清單"
        df["CB成交量"] = df["成交張數"].fillna(0).astype(int)
        df["CB均量"] = df["CB成交量"]
        df["溢價率"] = (df["債券市價"] / df["轉換價值"]) - 1
        df["股價轉換價比"] = (df["標的股價"] / df["轉換價格"]) - 1

    # 策略
    strategies = config.get("strategies", {})
    if not strategies:
        print("[錯誤] config.yaml 中沒有定義 strategies")
        sys.exit(1)

    if args.strategy:
        if args.strategy in strategies:
            strategies = {args.strategy: strategies[args.strategy]}
        else:
            print(f"[錯誤] 策略 '{args.strategy}' 不存在")
            print(f"[提示] 可用: {', '.join(strategies.keys())}")
            sys.exit(1)

    tg = config.get("telegram", {})
    bot_token = tg.get("bot_token", "")
    chat_id = tg.get("chat_id", "")
    can_send = (
        not args.dry_run and bot_token and chat_id
        and bot_token != "YOUR_BOT_TOKEN"
    )

    max_results = output_config.get("max_results", 15)

    for name, strategy in strategies.items():
        desc = strategy.get("description", "")
        print(f"\n{'─' * 44}")
        print(f"📋 策略: {name}")
        if desc:
            print(f"   {desc}")

        df_filtered = apply_strategy(df, strategy, strategy_name=name)

        if df_filtered.empty:
            print(f"  [結果] 沒有符合條件的可轉債")
            msg = f"📊 *{name}*\n📅 {datetime.now().strftime('%Y/%m/%d')}\n\n沒有符合條件的可轉債"
        else:
            print(f"  [結果] 篩選出 {len(df_filtered)} 檔")
            msg = format_telegram_message(name, desc, df_filtered, max_results, xls_date)

        print()
        print(msg)

        if output_config.get("save_csv", False) and not df_filtered.empty:
            csv_dir = output_config.get("csv_folder", "./output")
            os.makedirs(csv_dir, exist_ok=True)
            csv_path = os.path.join(csv_dir, f"{name.replace(' ','_')}.csv")
            out_cols = [
                "代號", "債券名稱", "股票代號", "有擔保",
                "債券市價", "標的股價", "股價來源",
                "轉換價格", "轉換價值", "溢折價",
                "賣回收益率", "成交張數", "流通張數",
                "到期剩餘天數", "賣回剩餘天數",
            ]
            existing = [c for c in out_cols if c in df_filtered.columns]
            df_filtered[existing].to_csv(csv_path, index=False, encoding="utf-8-sig")
            print(f"  [輸出] {csv_path}")

        if can_send:
            send_telegram(bot_token, chat_id, msg)
        elif args.dry_run:
            print("  [測試模式] 跳過 Telegram")

    print(f"\n{'=' * 44}")
    print("[完成] 所有策略執行完畢")


if __name__ == "__main__":
    main()
