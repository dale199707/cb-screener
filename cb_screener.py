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

    # 欄位對應
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

    # 溢價率（百分比 → 小數）
    df["溢價率"] = df["溢價率_raw"] / 100

    # 賣回收益率
    df["賣回收益率"] = pd.to_numeric(
        df["賣回收益率_raw"].replace("-", None), errors="coerce"
    )
    df["賣回收益率"] = df["賣回收益率"] / 100

    # 日期轉換
    for col in ["發行日", "到期日", "下一賣回日"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], format="%Y/%m/%d", errors="coerce")

    # 衍生欄位
    today = datetime.now()
    df["到期剩餘天數"] = (df["到期日"] - today).dt.days
    df["股價轉換價比"] = (df["標的股價"] / df["轉換價格"]) - 1

    # 擔保
    df["有擔保"] = df["擔保情形"].apply(
        lambda x: "有" if str(x) != "無" and pd.notna(x) else "無"
    )

    # CB均量
    df["CB均量"] = df["CB均量5日"].fillna(0)
    df["CB成交量"] = df["CB成交量"].fillna(0).astype(int)

    print(f"[資料] 成功取得 {len(df)} 檔可轉債（即時盤後資料）")
    return df


# ═══════════════════════════════════════════
#  2.5 用 yfinance 計算均線（87MA / 284MA）
# ═══════════════════════════════════════════
def fetch_ma_data(stock_codes: list) -> dict:
    """
    用 yfinance 逐檔抓歷史股價，計算 87MA 和 284MA
    回傳 {股票代號: {ma87, ma87_prev, ma284, bullish, ma_rising}}
    """
    try:
        import yfinance as yf
    except ImportError:
        print("[警告] 未安裝 yfinance，跳過均線計算（pip install yfinance）")
        return {}

    print(f"[均線] 準備計算 {len(stock_codes)} 檔標的股票的 87MA / 284MA...")

    result = {}
    failed = []

    for i, code in enumerate(stock_codes):
        # 先試上市 (.TW)，沒資料再試上櫃 (.TWO)
        df = pd.DataFrame()
        for suffix in [".TW", ".TWO"]:
            ticker = f"{code}{suffix}"
            try:
                temp = yf.Ticker(ticker).history(period="2y")
                if not temp.empty and len(temp) >= 284:
                    df = temp
                    break
            except Exception:
                continue

        if df.empty or len(df) < 284:
            failed.append(code)
            continue

        # 確保照日期排序
        df = df.sort_index()
        close = df["Close"].dropna()

        if len(close) < 284:
            failed.append(code)
            continue

        _calc_ma(close, code, result)

        # 每 20 檔印一次進度
        if (i + 1) % 20 == 0:
            print(f"[均線] 進度 {i+1}/{len(stock_codes)}...")

    bullish_count = sum(1 for v in result.values() if v["bullish"])
    print(f"[均線] 計算完成 {len(result)}/{len(stock_codes)} 檔")
    if failed:
        print(f"[均線] 無資料 {len(failed)} 檔: {', '.join(failed[:10])}{'...' if len(failed) > 10 else ''}")
    print(f"[均線] 多頭排列 {bullish_count} 檔")

    return result


def _calc_ma(close: pd.Series, symbol: str, result: dict):
    """計算單檔的 87MA / 284MA 並存入 result"""
    ma87 = close.rolling(87).mean()
    ma284 = close.rolling(284).mean()

    ma87_today = ma87.iloc[-1]
    ma87_yesterday = ma87.iloc[-2]
    ma284_today = ma284.iloc[-1]

    if pd.isna(ma87_today) or pd.isna(ma284_today):
        return

    result[symbol] = {
        "ma87": round(ma87_today, 2),
        "ma87_prev": round(ma87_yesterday, 2),
        "ma284": round(ma284_today, 2),
        "bullish": ma87_today > ma284_today,       # 多頭排列
        "ma_rising": ma87_today > ma87_yesterday,   # 均線上揚
    }


def apply_ma_to_df(df: pd.DataFrame, ma_data: dict) -> pd.DataFrame:
    """把均線資料合併到 CB DataFrame"""
    df = df.copy()

    df["MA87"] = df["股票代號"].map(lambda x: ma_data.get(x, {}).get("ma87"))
    df["MA284"] = df["股票代號"].map(lambda x: ma_data.get(x, {}).get("ma284"))
    df["多頭排列"] = df["股票代號"].map(
        lambda x: ma_data.get(x, {}).get("bullish", True)  # 無資料時保留
    )
    df["均線上揚"] = df["股票代號"].map(
        lambda x: ma_data.get(x, {}).get("ma_rising", True)  # 無資料時保留
    )

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

    # 全域條件 1：CB 當日成交量 ≥ 10
    before_global = len(result)
    result = result[result["CB成交量"] >= 10]
    after_global = len(result)
    if before_global != after_global:
        print(f"    CB成交量≥10: {before_global} → {after_global}")

    # 全域條件 2：均線多頭（87MA > 284MA）
    before_ma = len(result)
    result = result[result["多頭排列"]]
    after_ma = len(result)
    if before_ma != after_ma:
        print(f"    均線多頭(87MA>284MA): {before_ma} → {after_ma}")

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
#  4. 歷史紀錄（比對新增 CB）
# ═══════════════════════════════════════════
HISTORY_DIR = "history"


def load_previous_results(strategy_name: str) -> set:
    """讀取前一次篩選結果的 CB 代號"""
    path = os.path.join(HISTORY_DIR, f"{strategy_name}.json")
    if not os.path.exists(path):
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("codes", []))
    except (json.JSONDecodeError, KeyError):
        return set()


def save_current_results(strategy_name: str, codes: list, df: pd.DataFrame = None):
    """儲存本次篩選結果（代號清單 + 完整資料 JSON）"""
    os.makedirs(HISTORY_DIR, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")

    # 1) 儲存代號清單（用於隔日比對新增）
    path = os.path.join(HISTORY_DIR, f"{strategy_name}.json")
    data = {
        "date": today,
        "count": len(codes),
        "codes": codes,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # 2) 儲存完整資料（用於前端 / 歷史走勢）
    if df is not None and not df.empty:
        daily_dir = os.path.join(HISTORY_DIR, "daily")
        os.makedirs(daily_dir, exist_ok=True)
        daily_path = os.path.join(daily_dir, f"{today}_{strategy_name}.json")

        # 選擇要存的欄位
        export_cols = [
            "代號", "債券名稱", "股票代號", "有擔保",
            "債券市價", "標的股價", "轉換價格", "轉換價值",
            "溢價率", "CB成交量", "CB均量5日", "CB均量20日",
            "到期日", "TCRI", "MA87", "MA284",
        ]
        existing = [c for c in export_cols if c in df.columns]
        df_export = df[existing].copy()

        # 日期轉字串
        for col in df_export.columns:
            if pd.api.types.is_datetime64_any_dtype(df_export[col]):
                df_export[col] = df_export[col].dt.strftime("%Y-%m-%d")

        records = df_export.where(df_export.notna(), None).to_dict(orient="records")
        daily_data = {
            "date": today,
            "strategy": strategy_name,
            "count": len(records),
            "data": records,
        }
        with open(daily_path, "w", encoding="utf-8") as f:
            json.dump(daily_data, f, ensure_ascii=False, indent=2, default=str)
        print(f"  [歷史] 已存 {daily_path}")


# ═══════════════════════════════════════════
#  5. 格式化 Telegram 訊息
# ═══════════════════════════════════════════
def format_telegram_message(strategy_name, strategy_desc, df, new_codes=None):
    if new_codes is None:
        new_codes = set()
    today_str = datetime.now().strftime("%Y/%m/%d")
    lines = []
    lines.append(f"📊 *{strategy_name}*")
    if strategy_desc:
        lines.append(f"_{strategy_desc}_")
    new_count = sum(1 for _, r in df.iterrows() if str(r.get("代號", "")) in new_codes)
    if new_count > 0:
        lines.append(f"📅 {today_str}（篩出 {len(df)} 檔，🆕 {new_count} 檔新增）")
    else:
        lines.append(f"📅 {today_str}（篩出 {len(df)} 檔）")
    lines.append("─" * 24)

    # 新增的排最上面
    df = df.copy()
    df["_is_new"] = df["代號"].astype(str).isin(new_codes)
    df = df.sort_values("_is_new", ascending=False, kind="stable")

    for _, row in df.iterrows():
        code = str(row.get("代號", ""))
        name = str(row.get("債券名稱", ""))
        bond_price = row.get("債券市價", 0)
        premium = row.get("溢價率", 0)
        cb_vol = row.get("CB成交量", 0)
        avg5 = row.get("CB均量5日", 0) or 0
        avg20 = row.get("CB均量20日", 0) or 0
        cv = row.get("轉換價值", 0)
        stock_price = row.get("標的股價", 0)
        conv_price = row.get("轉換價格", 0)
        guaranteed = row.get("有擔保", "")
        expiry = row.get("到期日", None)
        ma87 = row.get("MA87", None)
        ma284 = row.get("MA284", None)

        emoji = "🟢" if premium < 0 else ("🟡" if premium < 0.05 else "🔴")
        shield = "🛡" if guaranteed == "有" else ""
        is_new = "🆕" if code in new_codes else ""
        exp_str = expiry.strftime("%Y/%m/%d") if pd.notna(expiry) else "-"
        ma_str = f"87MA {ma87:.1f} / 284MA {ma284:.1f}" if pd.notna(ma87) and pd.notna(ma284) else ""

        lines.append(f"{emoji} *{code} {name}* {shield}{is_new}")
        lines.append(f"  CB {bond_price:.2f} ｜溢價率 {premium:+.1%}")
        lines.append(f"  股價 {stock_price:.2f} ｜轉換價 {conv_price:.2f}")
        lines.append(f"  轉換價值 {cv:.2f} ｜到期 {exp_str}")
        lines.append(f"  成交 {int(cb_vol)} ｜5日均 {avg5:.0f} ｜20日均 {avg20:.0f}")
        if ma_str:
            lines.append(f"  📈 {ma_str}")
        lines.append("")

    lines.append("")
    lines.append("🟢折價 🟡微溢價(<5%) 🔴溢價 🛡擔保 📈均線多頭 🆕新增")

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

    # 先用 CB成交量 ≥ 10 預篩（減少需要抓均線的股票數量）
    df_active = df[df["CB成交量"] >= 10].copy()
    stock_codes = df_active["股票代號"].unique().tolist()
    print(f"[均線] CB成交量≥10 的標的股票: {len(stock_codes)} 檔")

    # 用 yfinance 計算均線
    ma_data = fetch_ma_data(stock_codes)
    if ma_data:
        df = apply_ma_to_df(df, ma_data)
    else:
        df["MA87"] = None
        df["MA284"] = None
        df["多頭排列"] = True   # 沒有均線資料時不篩
        df["均線上揚"] = True

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

        # 比對前一日結果，找出新增的 CB
        prev_codes = load_previous_results(name)
        current_codes = df_filtered["代號"].astype(str).tolist() if not df_filtered.empty else []
        new_codes = set(current_codes) - prev_codes

        if new_codes and prev_codes:  # prev_codes 為空代表第一次跑，不標新增
            print(f"  [新增] 🆕 {len(new_codes)} 檔: {', '.join(sorted(new_codes))}")
        elif not prev_codes:
            new_codes = set()  # 第一次跑，全部都不標新增

        if df_filtered.empty:
            print(f"  [結果] 沒有符合條件的可轉債")
            msg = (
                f"📊 *{name}*\n"
                f"📅 {datetime.now().strftime('%Y/%m/%d')}\n\n"
                f"沒有符合條件的可轉債"
            )
        else:
            print(f"  [結果] 篩選出 {len(df_filtered)} 檔")
            msg = format_telegram_message(name, desc, df_filtered, new_codes)

        # 儲存本次結果（代號 + 完整資料）
        save_current_results(name, current_codes, df_filtered)

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

    # 儲存 latest.json（前端用，包含所有策略的最新結果）
    latest_path = os.path.join(HISTORY_DIR, "latest.json")
    latest_data = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "strategies": {},
    }
    for name, strategy in config.get("strategies", {}).items():
        daily_path = os.path.join(
            HISTORY_DIR, "daily",
            f"{datetime.now().strftime('%Y-%m-%d')}_{name}.json"
        )
        if os.path.exists(daily_path):
            with open(daily_path, "r", encoding="utf-8") as f:
                latest_data["strategies"][name] = json.load(f)
    with open(latest_path, "w", encoding="utf-8") as f:
        json.dump(latest_data, f, ensure_ascii=False, indent=2)
    print(f"[前端] 已更新 {latest_path}")

    print(f"\n{'=' * 44}")
    print("[完成] 所有策略執行完畢")


if __name__ == "__main__":
    main()
