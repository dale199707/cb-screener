#!/usr/bin/env python3
"""
可轉債盤後篩選系統 v3
- 透過統一證券 CBAS API 一次取得所有可轉債即時資料
  （收盤價、股價、轉換價值、溢價率、成交量、均量等）
- 支援自訂篩選策略
- 透過 Telegram Bot 推播結果

Usage:
    python3 cb_screener.py                  # 正式執行
    python3 cb_screener.py --dry-run        # 測試模式（不發送 Telegram）
    python3 cb_screener.py --strategy 可轉債資優生
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import yaml


# ═══════════════════════════════════════════
#  1. 設定檔
# ═══════════════════════════════════════════
def load_config(config_path="config.yaml"):
    path = Path(config_path)
    if not path.exists():
        print(f"[錯誤] 找不到設定檔: {config_path}")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ═══════════════════════════════════════════
#  2. 從統一證券 CBAS API 取得所有可轉債資料
# ═══════════════════════════════════════════
CBAS_API_URL = "https://cbas16889.pscnet.com.tw/api/CbasQuote/GetIssuedCBSchedule"


def fetch_cb_data() -> pd.DataFrame:
    """一次抓取所有已發行可轉債的即時資料"""
    print("[資料] 正在從統一證券 CBAS API 抓取...")

    try:
        resp = requests.get(CBAS_API_URL, timeout=30, headers={
            "User-Agent": "Mozilla/5.0",
        })
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        print(f"[錯誤] API 請求失敗: {e}")
        sys.exit(1)

    if data.get("message") != "QuerySuccess":
        print(f"[錯誤] API 回傳異常: {data.get('message')}")
        sys.exit(1)

    results = data.get("result", [])
    if not results:
        print("[錯誤] API 回傳空資料")
        sys.exit(1)

    df = pd.DataFrame(results)

    # 欄位對應（API 欄位 → 程式內部欄位）
    col_map = {
        "bond_code": "代號",
        "underlying_bond": "債券名稱",
        "convert_target_code": "股票代號",
        "issue_date": "發行日",
        "expiry_date": "到期日",
        "circulation": "發行量億",
        "circulating_balance": "流通張數",
        "guarantee_situation": "擔保情形",
        "conversion_price": "轉換價格",
        "underlying_stock_market_price": "標的股價",
        "convertible_bond_market_price": "債券市價",
        "conversion_value": "轉換價值",
        "premium_rate": "溢價率_raw",
        "conversion_ratio": "轉換比率",
        "convertible_bond_turnover": "CB成交量",
        "average_daily_volume_of_convertible_bonds_5d": "CB均量5日",
        "average_daily_volume_of_convertible_bonds_20d": "CB均量20日",
        "latest_sale_date": "下一賣回日",
        "latest_sale_price": "下一賣回價",
        "sell_back_yield": "賣回收益率_raw",
        "tcri": "TCRI",
        "the_degree_of_price_inside_and_outside": "價內外程度",
    }

    df = df.rename(columns=col_map)

    # 數值轉換
    numeric_cols = [
        "發行量億", "流通張數", "轉換價格", "標的股價", "債券市價",
        "轉換價值", "溢價率_raw", "轉換比率", "CB成交量",
        "CB均量5日", "CB均量20日", "價內外程度",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # 溢價率：API 回的是百分比數字（如 40.42），轉成小數（0.4042）
    df["溢價率"] = df["溢價率_raw"] / 100

    # 賣回收益率：可能是 "-"
    df["賣回收益率"] = pd.to_numeric(
        df["賣回收益率_raw"].replace("-", None), errors="coerce"
    )
    df["賣回收益率"] = df["賣回收益率"] / 100  # 百分比轉小數

    # 日期轉換
    for col in ["發行日", "到期日", "下一賣回日"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], format="%Y/%m/%d", errors="coerce")

    # 衍生欄位
    today = datetime.now()
    df["到期剩餘天數"] = (df["到期日"] - today).dt.days

    # 股價對轉換價格的比率
    df["股價轉換價比"] = (df["標的股價"] / df["轉換價格"]) - 1

    # 擔保
    df["有擔保"] = df["擔保情形"].apply(
        lambda x: "有" if str(x) != "無" and pd.notna(x) else "無"
    )

    # CB均量（用 5日均量）
    df["CB均量"] = df["CB均量5日"].fillna(0)

    # 整數化
    df["CB成交量"] = df["CB成交量"].fillna(0).astype(int)

    print(f"[資料] 成功取得 {len(df)} 檔可轉債（即時盤後資料）")
    return df


# ═══════════════════════════════════════════
#  3. 篩選策略
# ═══════════════════════════════════════════
CUSTOM_FILTERS = {}


def register_filter(name):
    def decorator(func):
        CUSTOM_FILTERS[name] = func
        return func
    return decorator


@register_filter("可轉債資優生")
def filter_cb_honor(df):
    """
    CB市價 103~150
    且（股價在轉換價 -20%~+30% 或 CB市價 > 轉換價值）
    CB 5日均量 > 20
    """
    mask_price = (df["債券市價"] > 103) & (df["債券市價"] < 150)
    mask_stock = (df["股價轉換價比"] >= -0.20) & (df["股價轉換價比"] <= 0.30)
    mask_cv = df["債券市價"] > df["轉換價值"]

    result = df[mask_price & (mask_stock | mask_cv)]
    result = result[result["CB均量"] > 20]
    return result


@register_filter("突破轉換價")
def filter_breakthrough(df):
    """
    CB收盤價 > 轉換價值 且溢價率 < 5%
    股價 > 轉換價格 0%~10%
    CB 5日均量 > 50
    CB 日成交量 > 150 張
    """
    mask_premium = (df["債券市價"] > df["轉換價值"]) & (df["溢價率"] < 0.05)
    mask_stock = (df["股價轉換價比"] > 0) & (df["股價轉換價比"] < 0.10)
    mask_vol = df["CB成交量"] > 150
    mask_avg = df["CB均量"] > 50

    result = df[mask_premium & mask_stock & mask_vol & mask_avg]
    return result


def apply_strategy(df, strategy, strategy_name=""):
    result = df.copy()

    # 全域條件：CB 當日成交量 ≥ 10
    before_global = len(result)
    result = result[result["CB成交量"] >= 10]
    after_global = len(result)
    if before_global != after_global:
        print(f"    CB成交量≥10: {before_global} → {after_global}")

    if strategy_name in CUSTOM_FILTERS:
        before = len(result)
        result = CUSTOM_FILTERS[strategy_name](result)
        after = len(result)
        print(f"    自訂篩選: {before} → {after}")
    else:
        filters = strategy.get("filters", {})
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
            print(f"    {col_name}: {before} → {after}")

    sort_config = strategy.get("sort", {"by": "溢價率", "ascending": True})
    col = sort_config.get("by", "溢價率")
    asc = sort_config.get("ascending", True)
    if col in result.columns:
        result = result.sort_values(by=col, ascending=asc, na_position="last")

    return result


# ═══════════════════════════════════════════
#  4. 格式化 Telegram 訊息
# ═══════════════════════════════════════════
def format_telegram_message(strategy_name, strategy_desc, df):
    today_str = datetime.now().strftime("%Y/%m/%d")
    lines = []
    lines.append(f"📊 *{strategy_name}*")
    if strategy_desc:
        lines.append(f"_{strategy_desc}_")
    lines.append(f"📅 {today_str}（篩出 {len(df)} 檔）")
    lines.append("─" * 24)

    for _, row in df.iterrows():
        code = str(row.get("代號", ""))
        name = str(row.get("債券名稱", ""))
        bond_price = row.get("債券市價", 0)
        premium = row.get("溢價率", 0)
        cb_vol = row.get("CB成交量", 0)
        avg5 = row.get("CB均量5日", 0)
        avg20 = row.get("CB均量20日", 0)
        cv = row.get("轉換價值", 0)
        stock_price = row.get("標的股價", 0)
        conv_price = row.get("轉換價格", 0)
        guaranteed = row.get("有擔保", "")
        expiry = row.get("到期日", None)

        emoji = "🟢" if premium < 0 else ("🟡" if premium < 0.05 else "🔴")
        shield = "🛡" if guaranteed == "有" else ""
        exp_str = expiry.strftime("%Y/%m/%d") if pd.notna(expiry) else "-"

        lines.append(f"{emoji} *{code} {name}* {shield}")
        lines.append(f"  CB {bond_price:.2f} ｜溢價率 {premium:+.1%}")
        lines.append(f"  股價 {stock_price:.2f} ｜轉換價 {conv_price:.2f}")
        lines.append(f"  轉換價值 {cv:.2f} ｜到期 {exp_str}")
        lines.append(f"  成交 {int(cb_vol)} 張 ｜5日均量 {avg5:.0f} ｜20日均量 {avg20:.0f}")
        lines.append("")

    lines.append("")
    lines.append("🟢折價 🟡微溢價(<5%) 🔴溢價 🛡擔保")

    return "\n".join(lines)


# ═══════════════════════════════════════════
#  5. Telegram
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
#  6. 主程式
# ═══════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="可轉債盤後篩選系統 v3")
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="不發送 Telegram")
    parser.add_argument("--strategy", default=None, help="只跑特定策略")
    args = parser.parse_args()

    print("=" * 44)
    print("  可轉債盤後篩選系統 v3")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 44)

    config = load_config(args.config)

    # 從 CBAS API 取得所有可轉債資料
    df = fetch_cb_data()

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
        and bot_token not in ("YOUR_BOT_TOKEN", "GITHUB_WILL_INJECT")
    )

    output_config = config.get("output", {})

    for name, strategy in strategies.items():
        desc = strategy.get("description", "")
        print(f"\n{'─' * 44}")
        print(f"📋 策略: {name}")
        if desc:
            print(f"   {desc}")

        df_filtered = apply_strategy(df, strategy, strategy_name=name)

        if df_filtered.empty:
            print(f"  [結果] 沒有符合條件的可轉債")
            msg = (
                f"📊 *{name}*\n"
                f"📅 {datetime.now().strftime('%Y/%m/%d')}\n\n"
                f"沒有符合條件的可轉債"
            )
        else:
            print(f"  [結果] 篩選出 {len(df_filtered)} 檔")
            msg = format_telegram_message(name, desc, df_filtered)

        print()
        print(msg)

        # 存 CSV
        if output_config.get("save_csv", False) and not df_filtered.empty:
            csv_dir = output_config.get("csv_folder", "./output")
            os.makedirs(csv_dir, exist_ok=True)
            csv_path = os.path.join(csv_dir, f"{name.replace(' ', '_')}.csv")
            out_cols = [
                "代號", "債券名稱", "股票代號", "有擔保",
                "債券市價", "標的股價", "轉換價格", "轉換價值",
                "溢價率", "CB成交量", "CB均量", "CB均量5日", "CB均量20日",
                "到期剩餘天數", "TCRI",
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
