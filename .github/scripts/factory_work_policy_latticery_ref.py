"""Cross-repository extraction reference for issue #2870.

Behavior extracted to JoshCLWren/Latticery (pure domain, no host leaks):
- Dependency parsing: ``parse_dependency_numbers``, ``dependency_declarations``
- Unresolved blocking: ``has_unresolved_dependencies``
- Executable eligibility: ``is_executable``, ``eligibility_reason``
- Leading-reference cluster logic (deterministic, separator-aware)

Behavior intentionally retained in ComicPile (host-specific adapter/config layer):
- ``NON_EXECUTABLE_ISSUES`` (679, 1093, 1109) — product-specific exclusions
- Label-based eligibility (``factory:blocked``, ``ralph-status``, stage precedence)
- GitHub adapter wrappers (``issue_map``, ``open_numbers`` sourcing from API)
- ``MANUAL_ONLY_MARKER`` is retained as a generic domain marker in Latticery,
  but ComicPile's manual-only enforcement uses host issue-state context.

No production Factory controller has been switched to Latticery; this file
records the extraction boundary per acceptance criteria.

Dependency: #2874 (cross-repo delivery capability).
"""
from __future__ import annotations

# Delivery status (factory:53): branch prepared, push blocked by repo-scoped
# LATTICERY_TOKEN (github-actions[bot] lacks Latticery repo scope per config).
# Durable refs for manual/authorized delivery:
#   Branch: factory/2870-pure-dependency-executable-policy
#   Commit: bb4f52f
#   Source repo: /tmp/Latticery (cloned from https://github.com/JoshCLWren/Latticery)
#   PR target: JoshCLWren/Latticery
#   Dependency: #2874 (cross-repo delivery capability)
#   Issue: #2870
#
# Recommendation: use LATTICERY_TOKEN with repo scope to push branch and open PR.
# No production ComicPile Factory behavior switched; reference module kept minimal.
# Compatibility reference only — no production logic moved.
LATTICERY_DOMAIN_MODULES = [
    "latticery.dependency_policy",
    "latticery.executable_policy",
]

# Source behavior that remains host-specific in ComicPile.
HOST_SPECIFIC_BEHAVIOR = {
    "non_executable_issues": {679, 1093, 1109},
    "label_policies": ["factory:blocked", "ralph-status:blocked", "factory:building"],
    "adapter_layers": ["github_issue_map", "open_numbers_from_api"],
}
