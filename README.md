# 可轉債盤後篩選系統 — 使用說明

## 一、專案結構

```
cb-screener/
├── cb_screener.py      # 主程式
├── config.yaml         # 篩選條件設定檔（改這個就好）
├── requirements.txt    # Python 套件需求
├── data/               # 放 .xls 檔案的資料夾
│   └── CB_list_20260402.xls
└── output/             # 篩選結果 CSV 輸出
```

## 二、安裝步驟

### 1. 安裝 Python（如果還沒有的話）
到 https://www.python.org/downloads/ 下載 Python 3.10 以上版本。

### 2. 安裝套件
```bash
cd cb-screener
pip install -r requirements.txt
```

### 3. 設定 Telegram Bot

#### 步驟 A：建立 Bot
1. 在 Telegram 搜尋 `@BotFather`
2. 傳送 `/newbot`
3. 依提示輸入 Bot 名稱（例如 `我的可轉債篩選`）
4. 輸入 Bot username（例如 `my_cb_screener_bot`）
5. 取得 **Bot Token**（格式像 `123456789:ABCdefGHIjklMNO...`）

#### 步驟 B：取得你的 Chat ID
1. 在 Telegram 搜尋 `@userinfobot`
2. 傳送 `/start`
3. 它會回覆你的 **Chat ID**（一串數字）

#### 步驟 C：填入設定檔
打開 `config.yaml`，填入：
```yaml
telegram:
  bot_token: "123456789:ABCdefGHIjklMNO..."
  chat_id: "987654321"
```

**重要：** 先在 Telegram 中找到你的 Bot，傳送 `/start` 給它，Bot 才有權限傳訊息給你。

### 4. 放入 .xls 檔案
把券商給的 `.xls` 檔放到 `data/` 資料夾。
程式會自動找出最新的那份（依檔名排序）。

## 三、執行

### 測試模式（先試這個）
```bash
python cb_screener.py --dry-run
```
只會顯示篩選結果，不會發送 Telegram。

### 正式執行
```bash
python cb_screener.py
```

### 指定設定檔
```bash
python cb_screener.py -c my_other_config.yaml
```

## 四、修改篩選條件

打開 `config.yaml`，修改 `filters` 區塊：

```yaml
filters:
  # 溢折價率 ≤ 10%
  溢折價:
    max: 0.10

  # 債券市價 ≤ 110
  債券市價:
    max: 110

  # 賣回收益率 ≥ 0%
  賣回收益率:
    min: 0.0
```

### 可用的篩選欄位

| 欄位名稱 | 說明 | 範例 |
|---------|------|------|
| `溢折價` | 溢折價率（< 0 為折價） | `max: 0.05`（≤ 5%） |
| `債券市價` | 目前市場價格 | `max: 105` |
| `賣回收益率` | 持有到賣回日的年化報酬 | `min: 0.01`（≥ 1%） |
| `成交張數` | 當日成交量 | `min: 5` |
| `流通張數` | 市場流通總量 | `min: 1000` |
| `到期剩餘天數` | 距到期日天數 | `max: 730`（2年內） |
| `賣回剩餘天數` | 距下次賣回日天數 | `max: 365` |
| `轉換價值` | 轉換為股票的價值 | `min: 90` |
| `標的股價` | 底層股票價格 | - |
| `票面利率` | 票面利率 | - |

## 五、自動排程

### 方案 A：GitHub Actions（推薦 — 免費、零維護）

1. 在 GitHub 建一個 **私人倉庫**
2. 把整個 `cb-screener/` 推上去
3. 在倉庫的 Settings → Secrets 加入：
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. 建立 `.github/workflows/screen.yml`（見下方範例）
5. 每週五拿到新的 .xls 後，commit 到 `data/` 資料夾
6. GitHub Actions 會在週一～五 14:30 自動執行

```yaml
# .github/workflows/screen.yml
name: CB Screener

on:
  schedule:
    # UTC 06:30 = 台灣時間 14:30（週一到五）
    - cron: '30 6 * * 1-5'
  workflow_dispatch:  # 允許手動觸發

jobs:
  screen:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run screener
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: |
          # 用環境變數覆蓋 config 中的 Telegram 設定
          python -c "
          import yaml
          with open('config.yaml') as f:
              c = yaml.safe_load(f)
          import os
          c['telegram']['bot_token'] = os.environ['TELEGRAM_BOT_TOKEN']
          c['telegram']['chat_id'] = os.environ['TELEGRAM_CHAT_ID']
          with open('config.yaml', 'w') as f:
              yaml.dump(c, f, allow_unicode=True)
          "
          python cb_screener.py
```

### 方案 B：Windows 工作排程器

1. 打開「工作排程器」（搜尋 Task Scheduler）
2. 建立基本工作 → 觸發程序：每天 14:30
3. 動作：啟動程式
   - 程式：`python`
   - 引數：`C:\path\to\cb-screener\cb_screener.py`
   - 起始於：`C:\path\to\cb-screener`

### 方案 C：Mac / Linux crontab

```bash
# 每週一到五 14:30 執行
crontab -e
30 14 * * 1-5 cd /path/to/cb-screener && /usr/bin/python3 cb_screener.py >> /tmp/cb_screener.log 2>&1
```

## 六、常見問題

**Q: 出現 `ModuleNotFoundError: No module named 'xlrd'`**
A: 執行 `pip install xlrd`

**Q: Telegram 收不到訊息？**
A: 確認你有先傳 `/start` 給 Bot，且 `chat_id` 正確

**Q: 想篩選折價的可轉債？**
A: 在 config.yaml 設定 `溢折價: { max: 0 }`（溢折價 < 0 就是折價）

**Q: 想同時看上櫃和上市？**
A: 程式會自動讀取所有資料，不分上櫃上市
