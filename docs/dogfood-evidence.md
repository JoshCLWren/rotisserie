# Guarded self-dogfood evidence

## 2026-09-27 pre-activation review

The first three issue #9 stages were exercised without remote mutation or
worker execution.

| Stage | Repository | Payload digest | Projected graph | Decision |
|---|---|---|---|---|
| Fixture | `github.com/acme/oven` | `89e1d4d502ddb10b04502441e7f11a0869508fd2c2c1a88b3576bd1ed681a35e` | 3 work, 1 change, 2 checks, 2 reviews | No fresh implementation selected because the only open implementation already has a change. |
| Live read-only | `github.com/JoshCLWren/rotisserie` | `9829322415afc17dd5151cbfc8e8ba87dd783ee9ca1f45e7230e761edbae0274` | Roadmap phase issues #2–#11; no open pull requests | #9 is the sole executable phase because #8 is complete and #10 depends on #9. |
| Live dry-run | `github.com/JoshCLWren/rotisserie` | `9829322415afc17dd5151cbfc8e8ba87dd783ee9ca1f45e7230e761edbae0274` | Same snapshot | Planned an exact allowlisted label/state reconciliation for issue #9; credentials and transport were forbidden. |

The live acquisition deliberately excluded roadmap tracker #1 from executable
work. It included phase issues #2–#11 and encoded only the dependency edges
declared by their issue contracts. An initial projection that treated every
issue as work selected both #1 and #9; classifying #1 as coordination metadata
resolved that divergence without adding a GitHub label or issue number to
domain policy.

Every command reported `remote_mutation: false` and
`activation_approved: false`. The dry-run plan carried one target, issue #9,
and the complete proposed label set `rotisserie:canary`. No workflow, token,
branch, pull request, or remote repository state was changed.

This evidence does not authorize the canary stage. A maintainer must explicitly
approve activation before a write-capable workflow or remote executor is added.
