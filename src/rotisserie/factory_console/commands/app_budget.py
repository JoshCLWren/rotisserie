from __future__ import annotations

from rotisserie.factory_console.context import Context
from rotisserie.factory_console.github import GitHubError
from rotisserie.factory_console.service import app_budget, limits


def _spent(own: object) -> bool:
    core = own.get("core") if isinstance(own, dict) else None
    return not isinstance(core, dict) or not core.get("remaining")


def show_app_budget(context: Context, own: object) -> dict[str, object] | None:
    """The factory App's REST budget from the newest dispatcher quota gate.

    Reading it costs this credential a few REST calls, so it is skipped when
    the credential's own core budget is spent or unknown.
    """
    console = context.console
    now = context.clock.now()
    if _spent(own):
        console.line("Factory App REST budget: not read, because this credential's quota is spent.")
        return None
    try:
        found = app_budget.latest(context.github)
    except GitHubError as exc:
        console.warn(f"Cannot read the factory App's REST budget: {exc}")
        return None
    console.heading("Factory GitHub App REST budget (newest dispatcher run)")
    if found is None:
        console.line("  No recent dispatcher run printed its REST budget.")
        return None
    reset = found.reset_at()
    line = (
        f"  core: {found.remaining:,} / {found.limit:,} remaining, read "
        f"{app_budget.age(found, now)}; resets {limits.local_deadline(reset)}"
    )
    state = found.state(now)
    if state == "stale":
        console.line(line + " (that reset has passed, so the budget has refilled since)")
    elif state == "below_reserve":
        console.warn(line)
        console.line(f"  Below the dispatcher's {found.reserve:,} reserve: it skips dispatch.")
    elif state == "near_reserve":
        console.warn(line)
        console.line(f"  Near the dispatcher's {found.reserve:,} reserve, where it skips dispatch.")
    else:
        console.line(line)
    console.link("Dispatcher run", found.run_url)
    return found.to_dict(now)
