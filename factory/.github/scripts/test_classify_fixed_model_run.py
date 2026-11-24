"""Fixed-model log classification: delivery, idle and work outcomes."""

from __future__ import annotations

from classify_fixed_model_testing import ClassifierCase, classify


class ClassifierTests(ClassifierCase):


    def test_timed_out_agent_delivers_code_when_wrapper_releases_both_leases(self) -> None:
        """Worker 82's Nov 14 code survived a local agent timeout."""
        log = (
            "Factory 82 locked: source=z-ai model=glm-4.7-flash "
            "runtime=z-ai/glm-4.7-flash minute=:55 scheduler=dispatcher configured=true\n"
            "FIXED_MODEL_OPENCODE_OK\n"
            "[factory:82] checked out issue #4839 on factory/82-4839-z-ai\n"
            "[factory:82] agent exit status 124 for issue #4839\n"
            "[factory:82] opened/updated PR #5129 for issue #4839\n"
            "[factory:82] released issue #4839 to factory:unowned at factory:building (pr-opened-handoff)\n"
            "[factory:82] released pr #5129 to factory:unowned at factory:review (pr-opened-handoff)\n"
        )
        result = classify(log)
        self.assertEqual(result.outcome_class, "agent_timeout")
        self.assertTrue(result.persisted_work)
        self.assertEqual(result.delivery_outcome, "persisted")
        self.assertEqual(result.delivery_pr, "5129")
        incomplete = classify(log.replace(
            "released pr #5129 to factory:unowned", "failed to release pr #5129"
        ))
        self.assertEqual(incomplete.outcome_class, "agent_timeout")
        self.assertEqual(incomplete.delivery_outcome, "none")

    def test_work_session_without_smoke_proves_model_and_delivery(self) -> None:
        log = (
            "Factory 82 locked: source=z-ai model=glm-4.7-flash "
            "runtime=z-ai/glm-4.7-flash minute=:55 scheduler=dispatcher configured=true\n"
            "[factory:82] checked out issue #4839 on branch\n"
            "[factory:82] agent exit status 0 for issue #4839\n"
            "[factory:82] opened/updated PR #5129 for issue #4839\n"
            "[factory:82] released issue #4839 to factory:unowned\n"
            "[factory:82] released pr #5129 to factory:unowned\n"
        )
        result = classify(log)
        self.assertTrue(result.exact_model_proven)
        self.assertEqual(result.outcome_class, "success")
        self.assertEqual(result.delivery_outcome, "persisted")

    def test_no_assignment_is_idle_and_never_proves_model(self) -> None:
        log = (
            "Factory 46 locked: source=kilo-auto model=kilo-auto/free "
            "runtime=kilo/kilo-auto/free minute=:15 scheduler=dispatcher configured=true\n"
            "[factory:46] no control-plane assignment is leased to this worker; "
            "exiting without repo-wide selection\n"
        )
        result = classify(log)
        self.assertFalse(result.exact_model_proven)
        self.assertEqual(result.outcome_class, "no_work")

    def test_real_nvidia_410_retires_without_probe(self) -> None:
        log = (
            "Factory 11 locked: source=nvidia model=stepfun-ai/step-3.7-flash "
            "runtime=nvidia/stepfun-ai/step-3.7-flash minute=:15 "
            "scheduler=dispatcher configured=true\n"
            "[factory:11] checked out issue #4839 on factory/11-4839-nvidia\n"
            "[factory:11] agent exit status 1 for issue #4839\n"
            "HTTP 410 Gone\n"
            "NVIDIA permanently retired model: stepfun-ai/step-3.7-flash\n"
        )
        result = classify(log)
        self.assertEqual(result.outcome_class, "model_retired_410")
        self.assert_canonical(result)
        self.assertFalse(result.exact_model_proven)
        self.assertEqual(result.delivery_outcome, "none")
    def test_timeout_without_push_does_not_count_as_delivery(self) -> None:
        log = (
            "Factory 105 locked: source=z-ai model=glm-4.7-flash "
            "runtime=z-ai/glm-4.7-flash minute=:15 scheduler=dispatcher configured=true\n"
            "[factory:105] checked out pr #5123 on branch\n"
            "[factory:105] agent exit status 124 for pr #5123\n"
            "[factory:105] released pr #5123 to factory:unowned at factory:changes-requested\n"
        )
        result = classify(log)
        self.assertEqual(result.outcome_class, "agent_timeout")
        self.assertFalse(result.persisted_work)
        self.assertEqual(result.delivery_outcome, "none")

    def test_repair_delivery_requires_handoff_after_push(self) -> None:
        log = (
            "Factory 98 locked: source=z-ai model=glm-4.7-flash "
            "runtime=z-ai/glm-4.7-flash minute=:15 scheduler=dispatcher configured=true\n"
            "[factory:98] checked out pr #5122 on branch\n"
            "[factory:98] pushed repairs to PR #5122; handing it to the merge controller as factory:review\n"
        )
        self.assertEqual(classify(log).delivery_outcome, "none")
        complete = log + (
            "[factory:98] released pr #5122 to factory:unowned at factory:review (repairs-pushed-handoff)\n"
        )
        self.assertEqual(classify(complete).delivery_pr, "5122")

    def test_echoed_quota_script_and_branch_names_are_not_provider_errors(self) -> None:
        """Live Kilo logs contain conditional quota text even when no quota is hit."""
        lock = (
            "Factory 46 locked: source=kilo-auto model=auto/free "
            "runtime=kilo/auto/free minute=:15 scheduler=dispatcher configured=true\n"
        )
        log = (
            lock
            + "2026-11-13T15:14:37.049Z \x1b[36;1m    echo 'Kilo quota exhausted; ending before session' >&2\x1b[0m\n"
            + "2026-11-13T15:14:14.034Z  * [new branch] fix/3502-opencode-free-quota-classification\n"
            + "2026-11-13T15:14:46.212Z   remotes/origin/fix/provider-429-test\n"
            + '2026-11-13T15:15:09.755Z {"type":"tool_use","part":{"output":"quota exceeded in a readme"}}\n'
            + "FIXED_MODEL_OPENCODE_OK\n"
            + "[factory:46] checked out pr #5003 on work\n"
            + "opened/updated PR #5003 for issue #4842\n"
        )
        result = classify(log)
        self.assertEqual(result.outcome_class, "success")
        self.assertTrue(result.persisted_work)

    def test_git_commit_sha_429_does_not_pause_zai_or_kilo(self) -> None:
        """Live Z-AI run 43617062837 logged a commit hash ending in 429."""
        for source in ("z-ai", "kilo-auto"):
            log = self.BASE.replace("source=omniroute-opencode", f"source={source}")
            log += (
                "FIXED_MODEL_OPENCODE_OK\n"
                "2026-11-14T16:45:56.5705392Z 9cd7bc429 improve workflow\n"
                "[factory:54] repair attempt for PR #5060 produced no persisted change\n"
            )
            with self.subTest(source=source):
                self.assertEqual(classify(log).outcome_class, "no_work")

    def test_git_commit_sha_413_does_not_fake_http_throttle(self) -> None:
        log = (
            self.BASE
            + "FIXED_MODEL_OPENCODE_OK\n"
            + "2026-11-14T16:45:56.5705392Z abcdef413 improve workflow\n"
        )
        self.assertEqual(classify(log).outcome_class, "no_work")

    def test_explicit_http_429_is_provider_throttle_without_quota_evidence(self) -> None:
        log = (
            self.BASE.replace("source=omniroute-opencode", "source=z-ai")
            + "FIXED_MODEL_OPENCODE_OK\nHTTP 429 Too Many Requests\n"
        )
        self.assertEqual(classify(log).outcome_class, "provider_throttle")

    def test_explicit_account_daily_limit_remains_provider_quota(self) -> None:
        log = self.BASE + "FIXED_MODEL_OPENCODE_OK\n"
        log += "Rate limit exceeded: free-models-per-day-high-balance\n"
        self.assertEqual(classify(log).outcome_class, "provider_quota")

    def test_kilo_session_ids_cannot_fake_provider_throttle(self) -> None:
        """Real Kilo step IDs containing 413 must not cool a healthy provider."""
        log = (
            self.BASE
            + "FIXED_MODEL_OPENCODE_OK\n"
            + "[factory:46] checked out pr #5055 on factory/86-4737-nvidia\n"
            + '2026-11-14T16:29:02.8233288Z {"type":"step_start","part":{"id":"prt_1dae413e2001"}}\n'
            + '2026-11-14T16:29:03.0000000Z {"type":"step_finish","part":{"id":"prt_429example"}}\n'
            + '2026-11-14T16:29:04.0000000Z {"type":"tool_use","part":{"output":"quota and capacity examples in docs"}}\n'
            + "[factory:46] repair attempt for PR #5055 produced no persisted change\n"
        )
        result = classify(log)
        self.assertEqual(result.outcome_class, "no_work")
        self.assertFalse(result.persisted_work)

    def test_truncated_tool_json_cannot_cool_a_provider(self) -> None:
        """Incomplete tool JSON mentioning quota is not executed provider evidence."""
        log = (
            self.BASE
            + "FIXED_MODEL_OPENCODE_OK\n"
            + "[factory:46] checked out pr #5055 on factory/86-4737-nvidia\n"
            + '2026-11-14T16:27:40.7993231Z {"type":"tool_use","part":'
              '{"output":"quota, capacity, HTTP 429, and budget tests"'
            + "\n[factory:46] repair attempt for PR #5055 produced no persisted change\n"
        )
        result = classify(log)
        self.assertEqual(result.outcome_class, "no_work")
        self.assertFalse(result.persisted_work)

    def test_structured_provider_error_still_triggers_throttle(self) -> None:
        """A real structured error remains provider evidence after filtering."""
        log = self.BASE + (
            '2026-11-14T16:30:02.0000000Z '
            '{"type":"error","error":{"message":"HTTP 429 Too Many Requests"}}\n'
        )
        self.assertEqual(classify(log).outcome_class, "provider_throttle")

    def test_real_quota_error_still_classifies_after_command_filtering(self) -> None:
        log = self.BASE + (
            "2026-11-13T15:14:37.049Z \x1b[36;1m echo 'quota exhausted'\x1b[0m\n"
            "2026-11-13T15:14:39.001Z Error: API budget exhausted.\n"
        )
        self.assertEqual(classify(log).outcome_class, "provider_quota")

    def test_unrelated_numeric_ids_do_not_fake_provider_http_503(self) -> None:
        """An unrelated Actions comment/session ID is not a provider outage."""
        log = (
            self.BASE
            + "FIXED_MODEL_OPENCODE_OK\n"
            + "2026-11-14T16:43:18.8168504Z   FACTORY_HEARTBEAT_COMMENT_ID: 5030123456\n"
            + "2026-11-14T16:43:18.8168504Z   FACTORY_ATTEMPT_COMMENT_ID: 5029876543\n"
        )
        self.assertEqual(classify(log).outcome_class, "no_work")

    def test_real_http_503_remains_provider_failure(self) -> None:
        """Explicit HTTP error codes remain authoritative provider evidence."""
        log = self.BASE + "FIXED_MODEL_OPENCODE_OK\nHTTP 503\n"
        self.assertEqual(classify(log).outcome_class, "provider_failure")

    def test_controller_implementation_evidence_rejection_is_work_failure(self) -> None:
        """A trusted controller rejection is not a provider failure."""
        head = "0" * 40
        log = (
            self.BASE
            + "FIXED_MODEL_OPENCODE_OK\n"
            + f'2026-11-14T16:45:27.0752116Z {{"head": "{head}", '
              '"producer": "98", "status": "implementation-evidence-required"}\n'
            + "2026-11-14T16:45:27.1710943Z assignment complete\n"
        )
        self.assertEqual(classify(log).outcome_class, "work_failure")

    def test_productive(self) -> None:
        """Productive runs with proof token and persisted work are healthy."""
        result = classify(
            self.BASE
            + "FIXED_MODEL_OPENCODE_OK\n[factory:32] checked out issue #928 on x\n"
            + "opened/updated PR #1220 for issue #928\n"
        )
        self.assertEqual(result.outcome, "HEALTHY / PRODUCTIVE")
        self.assertEqual(result.outcome_class, "success")
        self.assertTrue(result.exact_model_proven)
        self.assertTrue(result.persisted_work)
        self.assertEqual((result.selected_kind, result.selected_number), ("issue", "928"))

    def test_idle_is_no_work(self) -> None:
        """Proof without selected or persisted work is canonical no_work."""
        result = classify(self.BASE + "FIXED_MODEL_OPENCODE_OK\nno selectable ordinary target\n")
        self.assertEqual(result.outcome, "HEALTHY / IDLE")
        self.assertEqual(result.outcome_class, "no_work")

    def test_rate_limited_is_provider_throttle(self) -> None:
        """Provider rate-limit responses map to canonical provider_throttle."""
        result = classify(self.BASE + "Factory fixed-model smoke\nError: Too Many Requests 429\n")
        self.assertEqual(result.outcome, "PROVIDER THROTTLE")
        self.assertEqual(result.outcome_class, "provider_throttle")
