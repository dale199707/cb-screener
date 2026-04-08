#!/usr/bin/env python3
"""
可轉債盤中報價擷取（13:25 執行）

在收盤前抓取所有可轉債的即時報價（價格 + 成交量），
存成 JSON 檔供 cb_screener.py 盤後使用。

盤後 TWSE API 的可轉債 z 欄位會被清空（變成 "-"），
所以必須在盤中抓取。

Usage:
    python3 cb_prefetch.py                  # 使用預設 config.yaml
    python3 cb_prefetch.py -c myconfig.yaml
"""

import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import yaml


def load_config(config_path="config.yaml"):
    path = Path(config_path)
    if not path.exists():
        print(f"[錯誤] 找不到設定檔: {config_path}")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def find_latest_xls(folder, prefix):
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
    return files_sorted[-1]


def read_cb_codes(filepath):
    """從券商清單讀取所有可轉債代號"""
    ext = Path(filepath).suffix.lower()
    if ext == ".xls":
        df = pd.read_excel(filepath, engine="xlrd", header=None, skiprows=4)
    else:
        df = pd.read_excel(filepath, header=None, skiprows=4)

    # 第一欄是代號
    codes = df.iloc[:, 0].astype(str)
    codes = codes[codes.str.match(r"^\d", na=False)].unique().tolist()
    return codes


def fetch_cb_prices(cb_codes):
    """盤中抓取可轉債的即時報價和成交量"""
    result = {}
    batch_size = 20

    print(f"[擷取] 準備抓取 {len(cb_codes)} 檔可轉債盤中報價...")

    for i in range(0, len(cb_codes), batch_size):
        batch = cb_codes[i : i + batch_size]

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
                    z = item.get("z", "")    # 最新成交價
                    y = item.get("y", "")    # 昨收
                    v = item.get("v", "")    # 累積成交量（張）
                    h = item.get("h", "")    # 最高
                    l = item.get("l", "")    # 最低
                    o = item.get("o", "")    # 開盤
                    t = item.get("t", "")    # 最新成交時間
                    d = item.get("d", "")    # 日期

                    # 取收盤價：優先用 z，沒有就用 y
                    price = None
                    price_source = None
                    if z and z != "-":
                        try:
                            price = float(z)
                            price_source = "盤中"
                        except ValueError:
                            pass
                    if price is None and y and y != "-":
                        try:
                            price = float(y)
                            price_source = "昨收"
                        except ValueError:
                            pass

                    # 成交量
                    volume = 0
                    if v and v != "-":
                        try:
                            volume = int(v)
                        except ValueError:
                            pass

                    if price is not None:
                        result[code] = {
                            "price": price,
                            "source": price_source,
                            "volume": volume,
                            "high": h,
                            "low": l,
                            "open": o,
                            "time": t,
                            "date": d,
                        }

        except requests.exceptions.RequestException as e:
            print(f"[警告] API 請求失敗 (batch {i//batch_size + 1}): {e}")

        if i + batch_size < len(cb_codes):
            time.sleep(3)

    found = len(result)
    print(f"[擷取] 成功取得 {found}/{len(cb_codes)} 檔可轉債報價")

    # 統計來源
    from_live = sum(1 for v in result.values() if v["source"] == "盤中")
    from_prev = sum(1 for v in result.values() if v["source"] == "昨收")
    print(f"[擷取] 盤中價 {from_live} 檔 ｜ 昨收價 {from_prev} 檔")

    return result


def main():
    parser = argparse.ArgumentParser(description="可轉債盤中報價擷取")
    parser.add_argument("-c", "--config", default="config.yaml")
    args = parser.parse_args()

    now = datetime.now()
    print("=" * 44)
    print("  可轉債盤中報價擷取")
    print(f"  {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 44)

    config = load_config(args.config)

    # 讀取可轉債代號
    xls_path = find_latest_xls(
        config["data"]["xls_folder"], config["data"]["file_prefix"]
    )
    print(f"[資料] 使用: {os.path.basename(xls_path)}")

    cb_codes = read_cb_codes(xls_path)
    print(f"[資料] 共 {len(cb_codes)} 檔可轉債")

    # 抓取盤中報價
    cb_data = fetch_cb_prices(cb_codes)

    # 存檔
    output_dir = config.get("output", {}).get("csv_folder", "./output")
    os.makedirs(output_dir, exist_ok=True)

    output = {
        "fetch_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "fetch_date": now.strftime("%Y%m%d"),
        "count": len(cb_data),
        "data": cb_data,
    }

    outpath = os.path.join(output_dir, "cb_prefetch.json")
    with open(outpath, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"[完成] 已存至 {outpath}")


if __name__ == "__main__":
    main()
