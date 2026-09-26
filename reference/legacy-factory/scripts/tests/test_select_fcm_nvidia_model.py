"""Tests for the defensive FCM NVIDIA model selector."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.select_fcm_nvidia_model import select_model, select_models


class SelectFcmNvidiaModelTests(unittest.TestCase):
    """Verify FCM output is sanitized and ranked before use."""

    def test_ignores_banner_other_providers_and_non_chat_models(self) -> None:
        """Only healthy NVIDIA chat models should be eligible."""
        payload = [
            {
                "provider": "groq",
                "status": "up",
                "modelId": "qwen/better-ranked",
                "tier": "S+",
                "stability": 100,
                "uptime": 100,
                "latestPing": 10,
            },
            {
                "provider": "nvidia",
                "status": "up",
                "modelId": "nvidia/nemotron-embed-1b",
                "tier": "S+",
                "stability": 100,
                "uptime": 100,
                "latestPing": 10,
            },
            {
                "provider": "nvidia",
                "status": "timeout",
                "modelId": "qwen/unavailable",
                "tier": "S+",
                "stability": 100,
                "uptime": 100,
                "latestPing": 10,
            },
            {
                "provider": "nvidia",
                "status": "up",
                "modelId": "openai/gpt-oss-20b",
                "tier": "S",
                "stability": 98,
                "uptime": 100,
                "latestPing": 500,
            },
        ]

        selected = select_model(f"  Pinging models...\n\n{json.dumps(payload)}")

        self.assertEqual(selected, "nvidia/openai/gpt-oss-20b")

    def test_prefers_tier_then_stability_uptime_and_latency(self) -> None:
        """Selection should remain deterministic as live model order changes."""
        payload = [
            {
                "provider": "nvidia",
                "status": "up",
                "modelId": "vendor/fast-a",
                "tier": "A+",
                "stability": 100,
                "uptime": 100,
                "latestPing": 1,
            },
            {
                "provider": "nvidia",
                "status": "up",
                "modelId": "vendor/stable-s",
                "tier": "S",
                "stability": 99,
                "uptime": 100,
                "latestPing": 900,
            },
            {
                "provider": "nvidia",
                "status": "up",
                "modelId": "vendor/less-stable-s",
                "tier": "S",
                "stability": 90,
                "uptime": 100,
                "latestPing": 100,
            },
        ]

        selected = select_model(json.dumps(payload))

        self.assertEqual(selected, "nvidia/vendor/stable-s")

    def test_rejects_output_without_a_healthy_candidate(self) -> None:
        """An unavailable review model should fail explicitly."""
        payload = [
            {
                "provider": "nvidia",
                "status": "timeout",
                "modelId": "vendor/down",
                "tier": "S",
            }
        ]

        with self.assertRaisesRegex(ValueError, "no healthy"):
            select_model(json.dumps(payload))

    def test_returns_all_candidates_in_fallback_order(self) -> None:
        """The workflow should be able to probe later candidates after an empty response."""
        payload = [
            {
                "provider": "nvidia",
                "status": "up",
                "modelId": "vendor/second",
                "tier": "S",
                "stability": 90,
                "uptime": 100,
                "latestPing": 100,
            },
            {
                "provider": "nvidia",
                "status": "up",
                "modelId": "vendor/first",
                "tier": "S+",
                "stability": 80,
                "uptime": 100,
                "latestPing": 200,
            },
            {
                "provider": "nvidia",
                "status": "up",
                "modelId": "vendor/invalid model",
                "tier": "S+",
                "stability": 100,
                "uptime": 100,
                "latestPing": 1,
            },
        ]

        self.assertEqual(
            select_models(json.dumps(payload)),
            ["nvidia/vendor/first", "nvidia/vendor/second"],
        )

    def test_review_workflow_probes_ranked_candidates_through_opencode(self) -> None:
        """Provider health alone must not select a model the review agent cannot invoke."""
        workflow = Path('.github/workflows/opencode.yml').read_text(encoding='utf-8')

        self.assertIn('Install pinned OpenCode release', workflow)
        self.assertIn('opencode run -m "$candidate"', workflow)
        self.assertIn("grep -q 'FCM_NVIDIA_MODEL_OK'", workflow)
        self.assertIn('Candidate failed the OpenCode compatibility probe', workflow)
        self.assertNotIn('https://integrate.api.nvidia.com/v1/chat/completions', workflow)
        self.assertIn('run: opencode github run', workflow)
        self.assertIn('MODEL: ${{ steps.model.outputs.model }}', workflow)
        self.assertNotIn('anomalyco/opencode/github@', workflow)
        self.assertGreaterEqual(
            workflow.count('^factory:(unowned|local|([1-9]|[1-3][0-9]|4[0-6]))$'),
            4,
        )


if __name__ == "__main__":
    unittest.main()
