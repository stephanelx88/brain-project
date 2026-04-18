"""Git operations for the brain repository."""

import subprocess
import brain.config as config


def commit(message: str) -> bool:
    """Stage all changes in ~/.brain and commit."""
    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=config.BRAIN_DIR,
            capture_output=True,
            check=True,
        )
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=config.BRAIN_DIR,
            capture_output=True,
        )
        if result.returncode == 0:
            return False  # nothing to commit

        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=config.BRAIN_DIR,
            capture_output=True,
            check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False
