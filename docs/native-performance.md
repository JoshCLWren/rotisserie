# Native policy and console performance

Rotisserie builds one PyO3 extension from the locked manifest under
`factory/native`. The installed CLI and overlaid factory scripts call it
through the existing Python functions. The factory stages the extension next
to the trusted eligibility source before a worker switches to a PR checkout.

The text implementation uses the Unicode categories in CPython 3.14 and
preserves Python whitespace, word boundaries and Unicode decimal semantics.
It preserves lone surrogate code points in snippets returned to Python. The
statistics implementation sorts ordinary float lists and tuples in Rust. Other
numeric sequences use Python's ordering and number operations from Rust, which
preserves arbitrary-size integers, Decimal and Fraction arithmetic, and NaN
ordering.

## Reproduce the benchmark

Use a normal locked development install and the same Python interpreter for
both runs. Create a baseline worktree at
`5529230fa796f8afd94bbaa4833253dd7556664c` and use its path for
`--source`. The benchmark blocks subprocess and socket operations and uses a
fixture GitHub adapter for the complete failing-check read path.

```sh
uv sync --locked
uv run --locked python scripts/benchmark-native.py --source /path/to/baseline
uv run --locked python scripts/benchmark-native.py
```

CPU time is the median of 11 warm batches. The default batch includes 384
issues, 48 PRs, 48 check logs and 384 duration histories. Compare each
workload's `result_sha256` across versions before interpreting the timings.

| Workload | Python CPU ms/batch | Native CPU ms/batch | Speedup |
| --- | ---: | ---: | ---: |
| Rank scheduling candidates | 59.21 | 20.42 | 2.90× |
| Build dependency graph and resolve blockers | 13.39 | 4.77 | 2.81× |
| Read and summarize failing checks | 31.44 | 2.43 | 12.93× |
| Calculate duration ranges | 2.40 | 1.16 | 2.07× |

All four fixture output digests matched on Linux x86-64 with CPython 3.14.7
and Rust 1.88. The logs use normal-sized synthetic job output and include
Unicode input. Production latency will also depend on network, store and
worker time.

## Remaining CPU work

In a fast-anything v0.9.0 scan of 25 batches, candidate construction accounted
for 51% of profiled elapsed time, with 12% in the acceptance-parent check.
A separate cProfile run also surfaced repeated label normalization in
scheduling and cycle detection in dependency graph construction. The
dependency parser still accounts for measurable work after the port and is
called from both scheduling and graph construction. Review reuse at those
boundaries before rewriting larger policy functions.
