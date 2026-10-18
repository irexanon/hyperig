import unittest

from modules.autodl import classify_instagram_url


class InstagramURLClassificationTests(unittest.TestCase):
    def test_reel(self):
        self.assertEqual(
            classify_instagram_url("https://www.instagram.com/reel/C0ffee123/"),
            ("reel", "https://www.instagram.com/reel/C0ffee123/"),
        )

    def test_post(self):
        category, url = classify_instagram_url("instagram.com/p/ABC123/?igsh=example")
        self.assertEqual(category, "post")
        self.assertTrue(url.startswith("https://instagram.com/p/ABC123/"))

    def test_story(self):
        self.assertEqual(
            classify_instagram_url("https://www.instagram.com/stories/sample_user/123456789/"),
            ("story", "https://www.instagram.com/stories/sample_user/123456789/"),
        )

    def test_canonical_highlight(self):
        category, _url = classify_instagram_url(
            "https://www.instagram.com/stories/highlights/17895485201104054/"
        )
        self.assertEqual(category, "highlight")

    def test_shared_highlight(self):
        category, _url = classify_instagram_url("https://www.instagram.com/s/AbCdEf/")
        self.assertEqual(category, "highlight")

    def test_profile(self):
        category, _url = classify_instagram_url("https://www.instagram.com/example_user/?igsh=example")
        self.assertEqual(category, "profile")

    def test_rejects_non_instagram_hosts(self):
        self.assertIsNone(classify_instagram_url("https://instagram.com.evil.example/p/ABC123/"))
        self.assertIsNone(classify_instagram_url("https://example.com/reel/ABC123/"))

    def test_rejects_unsupported_instagram_paths(self):
        self.assertIsNone(classify_instagram_url("https://www.instagram.com/explore/tags/python/"))

    def test_trailing_punctuation(self):
        category, _url = classify_instagram_url("(https://www.instagram.com/reel/C0ffee123/).")
        self.assertEqual(category, "reel")


if __name__ == "__main__":
    unittest.main()
