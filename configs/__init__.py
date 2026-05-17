import importlib
import os


def load_config() -> dict:
    """Load domain config based on DOMAIN env var. Defaults to india_colleges."""
    domain = os.getenv("DOMAIN", "india_colleges")
    try:
        module = importlib.import_module(f"configs.{domain}")
        return module.CONFIG
    except ModuleNotFoundError:
        raise ValueError(
            f"No config found for domain '{domain}'. "
            f"Available: india_colleges, hospitals, jobs"
        )
