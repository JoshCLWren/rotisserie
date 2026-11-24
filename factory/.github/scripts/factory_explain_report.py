"""Plain-text rendering of a work controller ``explain`` report."""

from __future__ import annotations

from typing import Any


def render_explain_report(report: dict[str, Any]) -> str:
    """Render an ``explain_issue`` report as operator-readable plain text.

    Args:
        report: The report returned by :func:`explain_issue`.

    Returns:
        Human-readable lines naming the verdict, each reason, and each gate.
    """
    verdict = 'eligible' if report['eligible'] else 'not eligible'
    lines = [f"#{report['issue']}: {verdict}"]
    lines.extend(f"  - {reason}" for reason in report['reasons'])
    prs = report.get('open_canonical_prs') or []
    lines.append(f"  open canonical PRs: {', '.join(f'#{n}' for n in prs) if prs else 'none'}")
    lines.append(f"  labels: {', '.join(report['labels']) or 'none'}")
    for name, value in report.get('dynamic_gates', {}).items():
        lines.append(f"  gate {name}: {value}")
    return '\n'.join(lines)
