# Compatibility, support, and deprecation

Rotisserie is currently `0.1.0.dev0`. No supported production release has been
published. Python 3.12, 3.13, and 3.14 are the tested development matrix. Newer
Python versions are not claimed supported merely because installation permits
them. Support currently means fixes on `main`, with no maintained release branch
or production service-level commitment.

## Public boundary

The intended integration surface is the installed `rotisserie` CLI, documented
command arguments and exit codes, schema-versioned TOML configuration, and
versioned serialized graph, operation, runtime, shadow, and adoption contracts.
See the [operator guide](operator.md) and
[adopter guide](adopter-integration.md) for individual schemas. Different
contracts have independent schema versions; package version is not a schema
version. Importable Python modules and adapter protocols remain experimental.
The prototype, archive, private helpers, and diagnostic message wording are not
compatibility interfaces.

Consumers must validate schema versions, required fields, repository scope,
and exact revisions. Unknown versions fail closed. Do not scrape logs, rely on
JSON key ordering, or treat a cutover decision as an already-applied host effect.

## Versioning and migrations

Package releases follow semantic versioning. Before 1.0, incompatible changes
require a minor version increment and explicit migration notes. Patch releases
preserve documented contracts. A breaking serialized change requires a new
schema version even during pre-release development. Additive fields must not
change existing safety semantics; consumers should tolerate additional output
fields only within a recognized version.

After 1.0, incompatible public changes require a major release. Deprecations
will identify the replacement, affected schema or command, migration procedure,
and planned removal version in the changelog. Ordinary removals should retain
one intervening minor release with the old interface documented as deprecated.
A security fix may shorten that window, with the reason and migration recorded.
No compatibility shim may bypass revision, identity, or authority validation.

Report reproducible bugs through the issue template with version, platform,
command, and redacted output. Use the private process in [SECURITY.md](../SECURITY.md)
for vulnerabilities. Release notes must state supported capabilities, Python
versions, known limitations, and migration requirements. Support for a published
release begins only when its release notes explicitly declare it.
