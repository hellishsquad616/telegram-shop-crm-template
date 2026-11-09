import unittest

from integrations.social_stats import SocialStatsClient, SocialStatsError, _follower_count


class SocialStatsClientTest(unittest.TestCase):
    def test_requires_platform_tokens(self) -> None:
        client = SocialStatsClient()
        self.assertFalse(client.tiktok_configured)
        self.assertFalse(client.instagram_configured)

    def test_parses_both_platform_count_names(self) -> None:
        self.assertEqual(_follower_count({"follower_count": 123}, "TikTok"), 123)
        self.assertEqual(
            _follower_count({"followers_count": "456"}, "Instagram"), 456
        )
        with self.assertRaises(SocialStatsError):
            _follower_count({}, "Instagram")


if __name__ == "__main__":
    unittest.main()
