import unittest
from scripts.publish_factory_quota import sanitize

class QuotaSanitizationTests(unittest.TestCase):
    def test_no_secret_or_private_metadata_leaks(self):
        sample = {"observed_at": "2026-10-10T13:00:00Z", "token": "secret",
          "installations": [{"public_alias": "factory-primary", "installation_id": 123,
            "private_repository": "secret-project", "token": "x",
            "core": {"limit": 5000, "remaining": 12, "used": 4988,
                     "reset_at": "2026-10-10T14:00:00Z", "private_url": "hidden"}}]}
        public = sanitize(sample)
        self.assertEqual(public["installations"][0]["core"]["remaining"], 12)
        self.assertNotIn("secret", str(public))
        self.assertNotIn("secret-project", str(public))
        self.assertNotIn("123", str(public))
    def test_bad_or_missing_measurements_not_healthy(self):
        sample = {"observed_at": "2026-10-10T13:00:00Z",
          "installations": [{"public_alias": "factory-primary", "core": {"limit": 5000}}]}
        public = sanitize(sample)
        self.assertNotIn("core", public["installations"][0])
        self.assertIsNone(public["exhaustion_incidents_24h"])

if __name__ == "__main__":
    unittest.main()
