import os
import tempfile
from pathlib import Path

FACTORY_ROOT = Path(__file__).resolve().parent
REFERENCE_ADOPTER = FACTORY_ROOT / "tests" / "fixtures" / "adopter" / "config.toml"

# The suite always runs as the reference adopter, including inside Rotisserie's
# own Actions where GITHUB_REPOSITORY names Rotisserie. Script constants read
# these at import, before any fixture runs.
os.environ["ROTISSERIE_FACTORY_ROOT"] = str(FACTORY_ROOT)
os.environ["ROTISSERIE_CONFIG"] = str(REFERENCE_ADOPTER)
os.environ["GITHUB_REPOSITORY"] = "JoshCLWren/comic-pile"

# A test that misses a stub must fail, never act on the real repository with
# the developer's stored gh login.
os.environ["GH_TOKEN"] = "invalid-test-token"
os.environ["GITHUB_TOKEN"] = "invalid-test-token"
os.environ["GH_CONFIG_DIR"] = tempfile.mkdtemp(prefix="factory-tests-gh-")
