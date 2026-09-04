"""LINE Messaging API 單向推播工具。

只使用 Channel access token；Channel secret 僅供 webhook 驗章，本專案不需要。
"""

import time
import uuid

import requests


LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
MAX_TEXT_CHARS = 4000
MAX_MESSAGES_PER_REQUEST = 5


def split_text_message(text: str, max_chars: int = MAX_TEXT_CHARS) -> list[str]:
    """依換行切割長訊息，並保留每一段的所有文字。"""
    if not text:
        return []

    parts = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > max_chars:
            if current:
                parts.append(current.rstrip("\n"))
                current = ""
            parts.append(line[:max_chars].rstrip("\n"))
            line = line[max_chars:]

        if len(current) + len(line) > max_chars and current:
            parts.append(current.rstrip("\n"))
            current = ""
        current += line

    if current:
        parts.append(current.rstrip("\n"))
    return [part for part in parts if part]


def send_line_message(
    channel_access_token: str,
    user_id: str,
    text: str,
    timeout: int = 15,
) -> bool:
    """送出 LINE push message；伺服器錯誤時以同一 retry key 重試一次。"""
    if not channel_access_token or not user_id:
        print("  [LINE] 缺少 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_USER_ID，跳過推播")
        return False

    parts = split_text_message(text)
    if not parts:
        return True

    for start in range(0, len(parts), MAX_MESSAGES_PER_REQUEST):
        batch = parts[start:start + MAX_MESSAGES_PER_REQUEST]
        retry_key = str(uuid.uuid4())
        headers = {
            "Authorization": f"Bearer {channel_access_token}",
            "Content-Type": "application/json",
            "X-Line-Retry-Key": retry_key,
        }
        payload = {
            "to": user_id,
            "messages": [{"type": "text", "text": part} for part in batch],
        }

        for attempt in range(2):
            try:
                response = requests.post(
                    LINE_PUSH_URL,
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                if attempt == 0:
                    time.sleep(1)
                    continue
                print(f"  [LINE] 推播失敗: {exc}")
                return False

            if response.status_code == 200:
                break
            if (
                response.status_code == 409
                and response.headers.get("x-line-accepted-request-id")
            ):
                # 前一次已被 LINE 接受，只是呼叫端沒收到成功回應；視為已送達，避免重複。
                break
            if response.status_code >= 500 and attempt == 0:
                time.sleep(1)
                continue

            detail = response.text.strip()[:300]
            print(f"  [LINE] 推播失敗 HTTP {response.status_code}: {detail}")
            return False
        else:
            return False

    print(f"  [LINE] 推播成功（{len(parts)} 段）")
    return True
