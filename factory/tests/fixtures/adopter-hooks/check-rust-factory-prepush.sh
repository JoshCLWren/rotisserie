#!/usr/bin/env bash
# Rust pre-push gate. Mirrors the two native Rust CI entrypoints with no
# database-dependent tests, so a worker cannot push unformatted or
# uncompilable Rust and use GitHub Actions as its first compiler.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

command -v cargo >/dev/null || { echo "cargo is required for Rust pre-push checks" >&2; exit 1; }
command -v rustup >/dev/null || { echo "rustup is required for pinned Rust toolchains" >&2; exit 1; }

# The root rust-toolchain.toml, Cargo workspace and single root Cargo.lock
# cover both the service tests and the deployed Vercel ingress. Check the
# whole workspace once so the tested and shipped graphs cannot drift.
cargo +stable fmt --all -- --check
cargo +stable clippy --workspace --locked --all-targets -- -D warnings
cargo +stable check --workspace --locked --all-targets
