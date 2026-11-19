# Measuring factory infrastructure activity

Run the normal trusted dispatcher with `mode=roster` and `measure=true` to
measure one actual cycle. `factory_state_metrics.py` attaches asyncpg's query
logger only when the job opts in. It records local operation categories,
duration and failure counts, without SQL text, parameters, credentials, model
logs or a database telemetry schema. The final step writes only the aggregate
to the Actions job summary. No measurement artifact is uploaded. Normal
scheduled/event cycles leave measurement off.

Counts distinguish SELECT/read statements, writes (including mutating CTEs),
DDL and transaction-control statements. These are actual driver calls, not
estimated rows or a subtraction of shared database-wide counters. Connection
handshake traffic and internal Postgres execution are outside this boundary.
A diagnostic file failure never changes the query result or lease behavior.

Compare cycles with the same dispatch mode, eligible work, worker availability
and quota conditions. Record assignments, commits, PRs and merges alongside
counts: a cheaper idle cycle is not evidence of improved useful delivery.
Measure GitHub calls, artifact counts/bytes and workflow handoffs separately.
The migration's before/after records belong here once the real cycles complete.
