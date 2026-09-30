# Local release artifact verification

Issue #11 remains open. This procedure produces unsigned candidate artifacts;
publication, signed provenance, SBOM, and security review are still pending.
It does not activate Actions or publish a package.

From a clean committed checkout, install the build backend and frontend from
the development lock and choose an output directory outside the checkout:

```sh
uv sync --locked
./scripts/verify
uv run --locked python scripts/verify-release.py --output /tmp/rotisserie-candidate
cd /tmp/rotisserie-candidate
sha256sum --check SHA256SUMS
```

The output directory must not already exist. The verifier rejects dirty or
untracked source, exports the exact Git commit into two separate directories,
and builds with the locked environment without isolated dependency resolution.
`SOURCE_DATE_EPOCH` uses the commit timestamp. Both wheel and source archive
must match byte for byte across builds; a wheel rebuilt from the source archive
must also match. Only verified artifacts are copied into the output directory.

`build-evidence.json` records the source commit, Python and uv versions, lock
hash, artifact hashes, and successful comparisons. `SHA256SUMS` covers the wheel
and source archive. This is local reproducibility evidence, not a signature or
an attestation from a trusted builder. It demonstrates repeatability in the
recorded environment, not reproducibility across operating systems or Python
versions. Verify the source commit against the intended release tag before
publication. Never attach credentials or environment dumps to release evidence.

A build mismatch fails the command before creating the output directory. Check
the lock, backend version, source timestamp, and environment; investigate the
mismatch rather than replacing checksums or disabling the comparison. An existing
output directory is preserved. Remove or choose a different candidate directory
explicitly when preparing another candidate.

The archived Factory is excluded from package distributions by the existing
sdist and wheel allowlists. It remains in Git as migration evidence. Release
notes must use the capabilities and limitations in the compatibility policy;
these candidate artifacts do not establish a supported release.
