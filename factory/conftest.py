import os
from pathlib import Path

FACTORY_ROOT = Path(__file__).resolve().parent
REFERENCE_ADOPTER = FACTORY_ROOT / "tests" / "fixtures" / "adopter" / "config.toml"

# Module-level script constants read these at import, before any fixture runs.
os.environ.setdefault("ROTISSERIE_FACTORY_ROOT", str(FACTORY_ROOT))
os.environ.setdefault("ROTISSERIE_CONFIG", str(REFERENCE_ADOPTER))
os.environ.setdefault("GITHUB_REPOSITORY", "JoshCLWren/comic-pile")
