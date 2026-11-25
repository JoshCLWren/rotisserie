// OpenCode 2 plugin, installed by install-opencode-v2.sh into the global plugins directory.
// OpenCode retries a provider 429 ten times (about 85 seconds) even when the refusal
// is a daily, monthly or credit quota. This plugin uses the documented session
// "retry" hook so a quota refusal fails on the first response and the factory
// classifies it at once. Rule:
//   - refusal text matches the factory_quota_reset.py credit, daily or monthly pattern,
//     or OpenCode typed it provider.quota, or HTTP 402: no retry;
//   - any other rate limit (429 or provider.rate-limit): at most one retry, waiting the
//     provider's Retry-After up to 30 seconds; a longer Retry-After fails at once;
//   - every other error (5xx, transport, overload): OpenCode's own decision, unchanged.
// A failure inside the plugin leaves OpenCode's decision unchanged.
import { closeSync, openSync, writeSync } from "node:fs"

// Same sources as CREDITS_RE, DAILY_RE and MONTHLY_RE in factory_quota_reset.py.
// tests/test_opencode_quota_retry_plugin.py fails when they drift.
export const QUOTA_PATTERNS = {
  credits: String.raw`insufficient balance|no resource package|please recharge|add usage credits|out of credits|balance is too low|credits? (?:depleted|exhausted)`,
  daily: String.raw`per[ -]day|\(RPD\)|free-models-per-day|daily (?:limit|quota)`,
  monthly: String.raw`monthly (?:usage )?limit|per month`,
}
const QUOTA_RES = Object.entries(QUOTA_PATTERNS).map(([kind, source]) => [kind, new RegExp(source, "i")])
export const THROTTLE_RETRIES = 1
export const THROTTLE_MAX_DELAY_MS = 30_000
const RESET_RE = /"?x-ratelimit-reset"?\s*[:=]\s*"?(\d+)/i

const refusalText = (error) => [error?.message ?? "", error?.response?.body ?? ""].join("\n")

export function quotaKind(error) {
  const text = refusalText(error)
  for (const [kind, pattern] of QUOTA_RES) if (pattern.test(text)) return kind
  if (error?.type === "provider.quota" || error?.status === 402) return "quota"
  return ""
}

const isRateLimit = (error) => error?.type === "provider.rate-limit" || error?.status === 429

// Returns the decision to use, or undefined to keep OpenCode's own decision.
export function retryDecision(event) {
  const { error, attempt, decision } = event
  const kind = quotaKind(error)
  if (kind) return { decision: { retry: false }, reason: `quota kind=${kind}` }
  if (!isRateLimit(error)) return undefined
  const retryAfter = error?.retryAfterMs
  if (attempt > 1 + THROTTLE_RETRIES) return { decision: { retry: false }, reason: "throttle retry used" }
  if (retryAfter !== undefined && retryAfter > THROTTLE_MAX_DELAY_MS)
    return { decision: { retry: false }, reason: `retry-after ${retryAfter}ms over ${THROTTLE_MAX_DELAY_MS}ms` }
  if (!decision?.retry) return undefined
  const delay = Math.min(Math.max(decision.delay, retryAfter ?? 0), THROTTLE_MAX_DELAY_MS)
  return { decision: { retry: true, delay }, reason: "throttle" }
}

export function refusalLine(event, verdict) {
  const { error } = event
  const reset = RESET_RE.exec(error?.response?.body ?? "")?.[1] ?? "none"
  const message = String(error?.message ?? "").replace(/\s+/g, " ").trim().slice(0, 300)
  const action = verdict.decision.retry ? `retry in ${verdict.decision.delay}ms` : "no retry"
  return (
    `[opencode-quota-guard] ${action} (${verdict.reason}): status=${error?.status ?? "none"} ` +
    `type=${error?.type ?? "none"} retry_after_ms=${error?.retryAfterMs ?? "none"} ` +
    `x_ratelimit_reset=${reset} message=${JSON.stringify(message)}\n`
  )
}

// Plugin stderr is not shown by `opencode run`: the plugin runs in the `serve --stdio`
// child. On Linux, write to the parent CLI's stderr so the line reaches the run log.
function report(line) {
  try {
    const fd = openSync(`/proc/${process.ppid}/fd/2`, "a")
    try {
      writeSync(fd, line)
    } finally {
      closeSync(fd)
    }
  } catch {}
}

export default {
  id: "factory-quota-retry",
  setup: async (ctx) => {
    try {
      await ctx.session.hook("retry", (event) => {
        try {
          const verdict = retryDecision(event)
          if (!verdict) return
          event.decision = verdict.decision
          report(refusalLine(event, verdict))
        } catch {}
      })
    } catch {}
  },
}
