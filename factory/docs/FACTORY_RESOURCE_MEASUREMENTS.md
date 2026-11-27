# Measuring factory infrastructure activity

Run the normal trusted dispatcher with `mode=roster` and `measure=true` to
measure one actual cycle. Each factory script that opens the store appends one
line to `FACTORY_STATE_METRICS_FILE` when it exits: its request count, the
store's own `rows_read` and `rows_written` counters, which are what the Turso
free plan bills, and its 20 statements with the most rows read. A statement is
its SQL shape, cut to 120 characters, with parameters left as `:name`
placeholders; no parameter value, key, credential or model log is recorded.
The final step writes the sum and the top 20 statement shapes, summed over the
pass's processes, to the Actions job summary. No measurement artifact is
uploaded.

The Status Pages job also publishes `db_usage.top_queries`: the database's top
queries from the Turso Platform API (`/databases/{db}/stats`), with every
string and number literal replaced by `?`. That list covers every job that uses
the store, not only the dispatcher. Normal scheduled/event cycles leave measurement off. A
diagnostic file failure never changes the query result or lease behavior.

Compare cycles with the same dispatch mode, eligible work, worker availability
and quota conditions. Record assignments, commits, PRs and merges alongside
counts: a cheaper idle cycle is not evidence of improved useful delivery.
Measure GitHub calls, artifact counts/bytes and workflow handoffs separately.
The migration's before/after records belong here once the real cycles complete.
