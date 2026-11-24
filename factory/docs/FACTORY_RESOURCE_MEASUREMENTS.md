# Measuring factory infrastructure activity

Run the normal trusted dispatcher with `mode=roster` and `measure=true` to
measure one actual cycle. Each factory script that opens the store appends one
line to `FACTORY_STATE_METRICS_FILE` when it exits: its request count and the
store's own `rows_read` and `rows_written` counters, which are what the Turso
free plan bills. It records no SQL text, parameters, credentials or model logs.
The final step writes only the sum to the Actions job summary. No measurement
artifact is uploaded. Normal scheduled/event cycles leave measurement off. A
diagnostic file failure never changes the query result or lease behavior.

Compare cycles with the same dispatch mode, eligible work, worker availability
and quota conditions. Record assignments, commits, PRs and merges alongside
counts: a cheaper idle cycle is not evidence of improved useful delivery.
Measure GitHub calls, artifact counts/bytes and workflow handoffs separately.
The migration's before/after records belong here once the real cycles complete.
