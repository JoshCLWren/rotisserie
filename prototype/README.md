# Policy prototype

This directory contains the first framework-free Rotisserie policy slice:

- explicit dependency declaration parsing;
- unresolved-dependency detection;
- deterministic executable-work eligibility;
- focused behavioral tests.

It is migration evidence, not the final package layout or public API. The
accepted behavior now has a compatibility test that projects parsed references
into explicit `rotisserie.domain` graph edges. Host text parsing remains outside
the pure graph model and will move behind an adapter boundary in a later phase.
