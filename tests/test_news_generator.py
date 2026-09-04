import unittest

import news_generator


class NewsGeneratorTests(unittest.TestCase):
    def test_validate_summary_accepts_exact_string_fields(self):
        summary = {key: f"{key} text" for key in news_generator.SUMMARY_KEYS}
        self.assertEqual(news_generator.validate_summary(summary), summary)

    def test_validate_summary_rejects_missing_field(self):
        with self.assertRaisesRegex(ValueError, "摘要缺少文字欄位"):
            news_generator.validate_summary({"overall": "ok"})

    def test_cb_code_fallback(self):
        self.assertEqual(news_generator.cb_code_to_stock("33245"), "3324")
        self.assertEqual(news_generator.cb_code_to_stock("811210"), "8112")

    def test_line_news_summary_contains_dashboard_links(self):
        summary = news_generator.format_line_news_summary(
            [{"title": "12345 測試一", "url_path": "news/12345.html"}],
            "https://example.com/dashboard",
        )
        self.assertIn("https://example.com/dashboard/news/12345.html", summary)
        self.assertNotIn("[12345", summary)


if __name__ == "__main__":
    unittest.main()
