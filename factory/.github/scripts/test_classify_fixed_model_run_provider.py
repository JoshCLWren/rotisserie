"""Fixed-model log classification: provider, quota and failure outcomes."""

from __future__ import annotations

from classify_fixed_model_testing import ClassifierCase, classify


class ClassifierProviderTests(ClassifierCase):
    def test_mistral_budget_exhausted_is_provider_quota(self) -> None:
        """Mistral 402 budget exhaustion is quota, not unknown_failure or throttle."""
        result = classify(
            self.BASE
            + "Smoke exact pinned model through OpenCode\n> build · codestral-latest\n"
            + "Error: API budget exhausted.\n##[error]Process completed with exit code 1.\n"
        )
        self.assertEqual(result.outcome, "PROVIDER QUOTA")
        self.assertEqual(result.outcome_class, "provider_quota")
        self.assert_canonical(result)

    def test_ollama_monthly_usage_limit_is_provider_quota(self) -> None:
        """Ollama monthly usage-limit 429s are quota exhaustion, not a throttle."""
        result = classify(
            self.BASE
            + "Factory fixed-model smoke\nError: you (joshisplutar) have reached your monthly "
            + "usage limit, upgrade for higher limits (HTTP 429)\n"
        )
        self.assertEqual(result.outcome_class, "provider_quota")

    def test_opencode_free_usage_limit_is_provider_quota(self) -> None:
        """An exhausted OpenCode free tier is quota, not a transient model failure."""
        lock = (
            "Factory 58 locked: source=opencode-free model=flint-40b-free "
            "runtime=opencode/flint-40b-free minute=:40 scheduler=dispatcher configured=true\n"
        )
        result = classify(
            lock
            + "Smoke exact pinned model through OpenCode\n"
            + "Error: Free usage limit reached. Connect a provider or subscribe "
            + "to OpenCode Go to keep going, or switch models.\n"
            + "##[error]Process completed with exit code 1.\n"
        )
        self.assertEqual(result.outcome, "PROVIDER QUOTA")
        self.assertEqual(result.outcome_class, "provider_quota")
        self.assertFalse(result.exact_model_proven)
        self.assertFalse(result.persisted_work)
        self.assert_canonical(result)

    def test_tpm_413_is_provider_throttle(self) -> None:
        """HTTP 413 TPM / request-too-large maps to provider_throttle."""
        result = classify(
            self.BASE
            + "Factory fixed-model smoke\n"
            + "Error: groq/openai/gpt-oss-20b: model — [413]: Request too large for model "
            + "`openai/gpt-oss-20b` on tokens per minute (TPM): Limit 8000, Requested 12027 "
            + "(HTTP 413)\n"
        )
        self.assertEqual(result.outcome, "PROVIDER THROTTLE")
        self.assertEqual(result.outcome_class, "provider_throttle")

    def test_native_intent_backing_unavailable_is_not_model_missing(self) -> None:
        """Backing catalog misses must not mark auto/coding:free as MODEL MISSING."""
        lock = (
            "Factory 68 locked: source=omniroute-free model=auto/coding:free "
            "runtime=omniroute/auto/coding:free minute=:30 scheduler=dispatcher configured=true\n"
        )
        result = classify(
            lock
            + "Factory fixed-model smoke\n"
            + "Error: groq/openai/gpt-oss-20b: model — [413]: Request too large (HTTP 413); "
            + "oc/north-mini-code-free: model — Model 'north-mini-code-free' is not available "
            + "in the active live catalog for provider 'opencode'. (HTTP 400)\n"
        )
        self.assertEqual(result.outcome_class, "provider_throttle")
        self.assertNotEqual(result.outcome, "MODEL MISSING")

    def test_native_intent_backing_only_unavailable_is_provider_failure(self) -> None:
        """Intent stays selectable when only a backing model is catalog-missing."""
        lock = (
            "Factory 68 locked: source=omniroute-free model=auto/coding:free "
            "runtime=omniroute/auto/coding:free minute=:30 scheduler=dispatcher configured=true\n"
        )
        result = classify(
            lock
            + "Factory fixed-model smoke\n"
            + "Model 'north-mini-code-free' is not available in the active live catalog "
            + "for provider 'opencode'. (HTTP 400)\n"
        )
        self.assertEqual(result.outcome_class, "provider_failure")
        self.assertNotEqual(result.outcome, "MODEL MISSING")

    def test_model_missing(self) -> None:
        """Missing pinned model exposure maps to MODEL MISSING."""
        self.assertEqual(
            classify(
                self.BASE + "Pinned OmniRoute model is not currently exposed: oc/big-pickle\n"
            ).outcome,
            "MODEL MISSING",
        )

    def test_nvidia_model_not_invokable_is_missing(self) -> None:
        """NVIDIA catalog entries that return 404 on invoke map to MODEL MISSING."""
        self.assertEqual(
            classify(
                self.BASE
                + "Pinned NVIDIA model is not currently invokable for this account: moonshotai/kimi-k2.6\n"
            ).outcome,
            "MODEL MISSING",
        )

    def test_explicit_http_model_retirement_is_unavailable(self) -> None:
        """HTTP 404 and 410 permanently identify an unavailable model only."""
        for response in (
            "HTTP 404",
            "HTTP status: 410",
            "410 Gone",
            "404 Not Found",
            "Model 'north-mini-code-free' is not available in the active live catalog",
        ):
            with self.subTest(response=response):
                result = classify(self.BASE + f"model invocation failed: {response}\n")
                self.assertEqual(result.outcome, "MODEL MISSING")
                self.assertEqual(result.outcome_class, "model_unavailable")

    def test_openrouter_daily_free_pool_exhaustion_is_quota(self) -> None:
        """Daily free-provider budget exhaustion must not be treated as transient throttle."""
        result = classify(
            self.BASE + "Error: Rate limit exceeded: free-models-per-day-high-balance.\\n"
        )
        self.assertEqual(result.outcome_class, "provider_quota")

    def test_model_stream_interruption_is_provider_failure(self) -> None:
        """Interrupted provider responses use the canonical transient provider failure."""
        result = classify(self.BASE + "FIXED_MODEL_OPENCODE_OK\nstream interrupted\n")
        self.assertEqual(result.outcome_class, "provider_failure")

    def test_control_plane_failure_is_distinct(self) -> None:
        """Controller lifecycle exceptions are not attributed to the model."""
        result = classify(self.BASE + "FIXED_MODEL_OPENCODE_OK\n[factory:32] lifecycle exception\n")
        self.assertEqual(result.outcome_class, "control_plane_failure")

    def test_assignment_read_failure_is_control_plane(self) -> None:
        """Explicit controller assignment failures do not poison model health."""
        result = classify(
            self.BASE
            + "FIXED_MODEL_OPENCODE_OK\n"
            + "[factory:32][source:x][model:y] released pr #1897 "
            + "(controller-assignment-read-failed)\n"
        )
        self.assertEqual(result.outcome_class, "control_plane_failure")

    def test_controller_names_in_agent_or_diff_output_are_not_control_plane(self) -> None:
        """Runs 45337725814, 45373867814, 45357706778: a diff listed the controller file."""
        for line in (
            "2026-11-24T06:06:26.1894297Z       - '.github/scripts/factory_completion_controller.py'\n",
            "2026-11-24T10:22:33.8046745Z -      - '.github/scripts/factory_completion_controller.py'\n",
            "the factory-work-controller lease failed in this fixture\n",
        ):
            with self.subTest(line=line):
                result = classify(
                    self.BASE
                    + "[factory:32] checked out pr #5737 on work\n"
                    + line
                    + "[factory:32][source:x][model:y] agent exit status 0 for pr #5737\n"
                )
                self.assertEqual(result.outcome_class, "no_work")

    def test_worker_control_plane_failure_line_is_control_plane(self) -> None:
        result = classify(
            self.BASE
            + "2026-11-24T06:06:26Z [factory:32][source:x][model:y] control_plane_failure: "
            + "PR #5 closed before trusted review-controller handoff\n"
        )
        self.assertEqual(result.outcome_class, "control_plane_failure")

    def test_work_failure_is_distinct(self) -> None:
        """Genuine failing tests remain repair work."""
        result = classify(self.BASE + "FIXED_MODEL_OPENCODE_OK\ntests failed\n")
        self.assertEqual(result.outcome_class, "work_failure")

    def test_no_change_repair_is_no_work(self) -> None:
        """Selected repairs without a diff use canonical no_work."""
        result = classify(
            self.BASE + "FIXED_MODEL_OPENCODE_OK\nchecked out pr #123 on branch\nno changes\n"
        )
        self.assertEqual(result.outcome_class, "no_work")

    def test_unknown_failure_fails_closed(self) -> None:
        """Unexplained process failures never become provider failures."""
        result = classify(
            self.BASE + "FIXED_MODEL_OPENCODE_OK\nProcess completed with exit code 1\n"
        )
        self.assertEqual(result.outcome_class, "unknown_failure")

    def test_environment_failure_is_distinct(self) -> None:
        """Runner and checkout failures do not poison model health."""
        result = classify(self.BASE + "checkout failed\n")
        self.assertEqual(result.outcome_class, "environment_failure")

    def test_trusted_guard_is_work_scoped(self) -> None:
        """Internal work guards do not permanently poison a model."""
        result = classify(self.BASE + "trusted guard rejected unsafe repair\n")
        self.assertEqual(result.outcome_class, "work_failure")

    def test_explicit_model_policy_violation_is_model_scoped(self) -> None:
        """Explicit provider/model policy evidence uses the model-scoped class."""
        result = classify(self.BASE + "provider rejected request: content policy violation\n")
        self.assertEqual(result.outcome_class, "model_policy_violation")

    def test_wrapper_deadline_does_not_poison_provider_health(self) -> None:
        """A local 124 beats incidental rate-limit text in agent logs."""
        result = classify(
            self.BASE
            + "FIXED_MODEL_OPENCODE_OK\n"
            + "Review docs discuss rate limit and quota decisions\n"
            + "[factory:32][source:omniroute-opencode][model:oc/big-pickle] "
            "agent exit status 124 for issue #4822\n"
        )
        self.assertEqual(result.outcome_class, "agent_timeout")

    def test_explicit_daily_account_quota_still_wins_over_local_deadline(self) -> None:
        result = classify(
            self.BASE
            + "Rate limit exceeded: free-models-per-day-high-balance.\n"
            + "[factory:32] agent exit status 124 for issue #4822\n"
        )
        self.assertEqual(result.outcome_class, "provider_quota")

    def test_timeout_after_proof_is_provider_failure(self) -> None:
        """A provider timeout after proof is a canonical provider failure."""
        result = classify(
            self.BASE + "FIXED_MODEL_OPENCODE_OK\nProcess completed with exit code 124\n"
        )
        self.assertEqual(result.outcome_class, "provider_failure")

    def test_cancellation_is_control_plane_failure(self) -> None:
        """Job cancellation is control-plane evidence, not provider health evidence."""
        result = classify(self.BASE + "FIXED_MODEL_OPENCODE_OK\nThe operation was canceled.\n")
        self.assertEqual(result.outcome_class, "control_plane_failure")

    def test_unconfigured(self) -> None:
        """Unconfigured lanes never invoke the model and stay unproven."""
        log = (
            "Factory 6 locked: source=nvidia model=z-ai/glm-5.2 "
            "runtime=nvidia/z-ai/glm-5.2 minute=:00 scheduler=slot-00 configured=false\n"
        )
        result = classify(log)
        self.assertEqual(result.outcome, "NOT YET PROVEN")
        self.assert_canonical(result)

    def test_omniroute_setup_failure_is_unproven(self) -> None:
        """OmniRoute setup failures before proof remain NOT YET PROVEN."""
        result = classify(self.BASE + "docker: Error response from daemon\n")
        self.assertEqual(result.outcome, "NOT YET PROVEN")
        self.assertEqual(result.outcome_class, "environment_failure")

    def test_failed_exact_smoke_is_provider_failure(self) -> None:
        """Smoke that reaches the provider without the proof token is a provider failure."""
        result = classify(
            self.BASE
            + "Smoke exact pinned model through OpenCode\nError: unexpected provider response\n"
        )
        self.assertEqual(result.outcome, "PROVIDER FAILURE")
        self.assertEqual(result.outcome_class, "provider_failure")

    def test_gha_timestamp_milliseconds_are_not_rate_limits(self) -> None:
        """Bare 429 inside GitHub Actions timestamps must not throttle a model."""
        nvidia = (
            "Factory 21 locked: source=nvidia model=google/gemma-4-31b-it "
            "runtime=nvidia/google/gemma-4-31b-it minute=:50 scheduler=dispatcher "
            "configured=true\n"
        )
        result = classify(
            nvidia
            + "2026-09-14T04:55:12.4291234Z Probe pinned NVIDIA model before OpenCode smoke\n"
            + "Pinned NVIDIA model probe failed HTTP 401\n"
        )
        self.assertNotEqual(result.outcome_class, "provider_throttle")
        self.assertEqual(result.outcome_class, "provider_failure")

    def test_capacity_script_echo_is_not_rate_limit(self) -> None:
        """Factory capacity wording in runner logs is not provider throttle evidence."""
        result = classify(
            self.BASE
            + "Configure selected OmniRoute capacity\n"
            + 'OmniRoute free-entry capacity: {"remaining": 2}\n'
            + "Smoke exact pinned model through OpenCode\n"
            + "Error: unexpected provider response\n"
        )
        self.assertNotEqual(result.outcome_class, "provider_throttle")
        self.assertEqual(result.outcome_class, "provider_failure")

    def test_model_at_capacity_is_provider_throttle(self) -> None:
        """Explicit provider capacity exhaustion remains provider_throttle."""
        result = classify(self.BASE + "Factory fixed-model smoke\nThe model is at capacity\n")
        self.assertEqual(result.outcome, "PROVIDER THROTTLE")
        self.assertEqual(result.outcome_class, "provider_throttle")

    def test_nvidia_probe_hard_fail_is_provider_failure(self) -> None:
        """Non-transient NVIDIA probe hard-fail is opaque without this class."""
        nvidia = (
            "Factory 21 locked: source=nvidia model=google/gemma-4-31b-it "
            "runtime=nvidia/google/gemma-4-31b-it minute=:50 scheduler=dispatcher "
            "configured=true\n"
        )
        result = classify(nvidia + "Pinned NVIDIA model probe failed HTTP 401\n")
        self.assertEqual(result.outcome, "PROVIDER FAILURE")
        self.assertEqual(result.outcome_class, "provider_failure")

    def test_legacy_http_000_probe_hard_fail_is_provider_failure(self) -> None:
        """HTTP 000 hard-fail plus curl timeout must not stay unknown_failure."""
        nvidia = (
            "Factory 21 locked: source=nvidia model=google/gemma-4-31b-it "
            "runtime=nvidia/google/gemma-4-31b-it minute=:50 scheduler=dispatcher "
            "configured=true\n"
        )
        result = classify(
            nvidia
            + "curl: (28) Operation timed out after 120000 ms with 0 bytes received\n"
            + "Pinned NVIDIA model probe failed HTTP 000\n"
        )
        self.assertEqual(result.outcome_class, "provider_failure")
        self.assertNotEqual(result.outcome_class, "unknown_failure")

    def test_probe_curl_timeout_then_smoke_proof_is_idle(self) -> None:
        """Retried HTTP 000 probe timeouts must not poison a later proof token."""
        nvidia = (
            "Factory 21 locked: source=nvidia model=google/gemma-4-31b-it "
            "runtime=nvidia/google/gemma-4-31b-it minute=:50 scheduler=dispatcher "
            "configured=true\n"
        )
        result = classify(
            nvidia
            + "curl: (28) Operation timed out after 45000 ms with 0 bytes received\n"
            + "NVIDIA probe transient HTTP 000 curl_exit 28 (attempt 1/3); retrying after 5s\n"
            + "NVIDIA probe succeeded for google/gemma-4-31b-it\n"
            + "FIXED_MODEL_OPENCODE_OK\n"
        )
        self.assertEqual(result.outcome, "HEALTHY / IDLE")
        self.assertEqual(result.outcome_class, "no_work")

    def test_all_representative_results_use_canonical_taxonomy(self) -> None:
        """Regression guard forbids legacy runtime outcome classes."""
        logs = (
            self.BASE + "FIXED_MODEL_OPENCODE_OK\nno selectable ordinary target\n",
            self.BASE + "FIXED_MODEL_OPENCODE_OK\ntests failed\n",
            self.BASE + "FIXED_MODEL_OPENCODE_OK\nprovider error 503\n",
            self.BASE + "Error 429 Too Many Requests\n",
            self.BASE + "Request too large tokens per minute (TPM) HTTP 413\n",
            self.BASE + "model invocation failed: HTTP 404\n",
            self.BASE + "content policy violation\n",
            self.BASE + "checkout failed\n",
            self.BASE + "FIXED_MODEL_OPENCODE_OK\nlifecycle failure\n",
            self.BASE + "FIXED_MODEL_OPENCODE_OK\nProcess completed with exit code 1\n",
        )
        for log in logs:
            with self.subTest(log=log):
                self.assert_canonical(classify(log))
