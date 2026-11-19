#!/usr/bin/env node
// Canonical launcher ships with the standalone Codex skill; keep one implementation.
import { main } from '../.agents/skills/crouton-factory-codex/scripts/local-factory-acp-review.mjs';
await main();
