import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("factory_quota_observer", Path(__file__).with_name("factory-quota-observer.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class FactoryQuotaObserverTests(unittest.TestCase):
    def test_sanitizes_response_and_converts_reset(self):
        raw = {"secret": "do-not-copy", "resources": {
            "core": {"limit": 5000, "remaining": 0, "used": 5000, "reset": 1791643115, "unknown": "secret"},
            "graphql": {"limit": 5000, "remaining": 4000, "used": 1000, "reset": 1791643115},
            "search": {"limit": 30, "remaining": 27, "used": 3, "reset": 1791643115},
        }}
        result = module.snapshot(raw, "2026-10-10T14:00:00Z")
        self.assertNotIn("secret", str(result))
        self.assertEqual(result["resources"]["core"]["remaining"], 0)
        self.assertTrue(result["resources"]["core"]["reset_at"].endswith("Z"))
        self.assertEqual(set(result["resources"]), {"core", "graphql", "search"})

    def test_missing_bucket_is_not_reported_healthy(self):
        result = module.snapshot({"resources": {"core": {"remaining": 5000}}}, "2026-10-10T14:00:00Z")
        self.assertEqual(result["resources"], {})

if __name__ == "__main__":
    unittest.main()
