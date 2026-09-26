# Rotisserie

Rotisserie is a graph-engineering platform for letting AI workers cook on your
issues. It is being extracted from the production-proven ComicPile Factory.

## Migration status

This repository currently contains a copy-first snapshot of the Factory code,
tests, workflows, prompts, and policy documents from ComicPile. The source
Factory remains in place and operational; this phase does not cut ComicPile
over to Rotisserie and did not use the Factory to perform the migration.

See [MIGRATION.md](MIGRATION.md) for provenance, scope, and verification notes.

Imported ComicPile workflows are retained as non-executable migration
references. Rotisserie's sole active GitHub Action is a read-only CI workflow;
the autonomous runtime will be enabled only after its repository-specific
contracts have been ported.
