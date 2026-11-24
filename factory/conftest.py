import os
import sys
import tempfile
from pathlib import Path

FACTORY_ROOT = Path(__file__).resolve().parent
# Runtime scripts import their siblings (such as factory_adopter); tests load
# some of them by file path, outside their own directory.
sys.path.insert(0, str(FACTORY_ROOT / ".github" / "scripts"))
REFERENCE_ADOPTER = FACTORY_ROOT / "tests" / "fixtures" / "adopter" / "config.toml"

# The suite always runs as the reference adopter, including inside Rotisserie's
# own Actions where GITHUB_REPOSITORY names Rotisserie. Script constants read
# these at import, before any fixture runs.
os.environ["ROTISSERIE_FACTORY_ROOT"] = str(FACTORY_ROOT)
os.environ["ROTISSERIE_CONFIG"] = str(REFERENCE_ADOPTER)
os.environ["GITHUB_REPOSITORY"] = "acme/widgets"
# Worker entry points export the prefix before sourcing helpers; tests that
# run one extracted shell function rely on the same export.
os.environ.pop("FACTORY_MARKER_PREFIX", None)
import factory_adopter  # noqa: E402

os.environ["FACTORY_MARKER_PREFIX"] = factory_adopter.marker_prefix()

# A test that misses a stub must fail, never act on the real repository with
# the developer's stored gh login.
os.environ["GH_TOKEN"] = "invalid-test-token"
os.environ["GITHUB_TOKEN"] = "invalid-test-token"
os.environ["GH_CONFIG_DIR"] = tempfile.mkdtemp(prefix="factory-tests-gh-")

# The suite has no outside-agent claim store. Say so explicitly: inside
# Actions, an unset setting makes claim-gated controller steps hold closed.
# Tests of that rule set or clear it themselves.
os.environ["FACTORY_NEON_CONFIGURED"] = "false"
