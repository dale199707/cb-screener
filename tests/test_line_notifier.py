import unittest
from unittest.mock import Mock, patch

import line_notifier


class LineNotifierTests(unittest.TestCase):
    def test_split_text_message_preserves_content(self):
        text = "第一行\n" + ("長" * 25) + "\n最後一行"
        parts = line_notifier.split_text_message(text, max_chars=10)
        self.assertTrue(all(len(part) <= 10 for part in parts))
        self.assertEqual("".join(parts).replace("\n", ""), text.replace("\n", ""))

    @patch("line_notifier.requests.post")
    def test_send_line_message_uses_push_endpoint_and_bearer_token(self, post):
        post.return_value = Mock(status_code=200)

        sent = line_notifier.send_line_message("token-value", "user-value", "測試訊息")

        self.assertTrue(sent)
        kwargs = post.call_args.kwargs
        self.assertEqual(post.call_args.args[0], line_notifier.LINE_PUSH_URL)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer token-value")
        self.assertIn("X-Line-Retry-Key", kwargs["headers"])
        self.assertEqual(kwargs["json"], {
            "to": "user-value",
            "messages": [{"type": "text", "text": "測試訊息"}],
        })

    @patch("line_notifier.requests.post")
    def test_missing_credentials_never_calls_api(self, post):
        self.assertFalse(line_notifier.send_line_message("", "", "測試訊息"))
        post.assert_not_called()

    @patch("line_notifier.time.sleep")
    @patch("line_notifier.requests.post")
    def test_retry_accepts_already_processed_response(self, post, sleep):
        post.side_effect = [
            line_notifier.requests.RequestException("connection lost"),
            Mock(
                status_code=409,
                headers={"x-line-accepted-request-id": "request-id"},
            ),
        ]

        self.assertTrue(
            line_notifier.send_line_message("token-value", "user-value", "測試訊息")
        )
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
