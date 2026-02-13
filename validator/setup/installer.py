"""
Setup and installation module.

Handles:
- Isaac Lab/Sim verification
- Environment downloading via nepher
- Evaluation repo cloning/updating
- Tournament config downloading
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, List

from nepher_core.api import TournamentAPI
from nepher_core.config import ValidatorConfig
from nepher_core.config.loader import save_yaml
from nepher_core.utils.helpers import run_command, run_command_async
from nepher_core.utils.logging import get_logger

logger = get_logger(__name__)


class SetupError(Exception):
    """Raised when setup fails."""
    pass


def verify_isaac_installation(
    expected_lab_version: str,
    expected_sim_version: str,
) -> bool:
    """
    Verify Isaac Lab and Isaac Sim are installed with correct versions.
    
    Args:
        expected_lab_version: Expected Isaac Lab version
        expected_sim_version: Expected Isaac Sim version
        
    Returns:
        True if versions match
    """
    # Check ISAACLAB_PATH environment variable
    isaaclab_path = os.environ.get("ISAACLAB_PATH")
    if not isaaclab_path:
        logger.warning("ISAACLAB_PATH not set")
        return False
    
    # Check ISAACSIM_PATH environment variable
    isaacsim_path = os.environ.get("ISAACSIM_PATH")
    if not isaacsim_path:
        logger.warning("ISAACSIM_PATH not set")
        return False
    
    # Verify paths exist
    if not Path(isaaclab_path).exists():
        logger.warning(f"Isaac Lab path does not exist: {isaaclab_path}")
        return False
    
    if not Path(isaacsim_path).exists():
        logger.warning(f"Isaac Sim path does not exist: {isaacsim_path}")
        return False
    
    logger.info(f"Isaac Lab path: {isaaclab_path}")
    logger.info(f"Isaac Sim path: {isaacsim_path}")
    
    # TODO: Add version checking when Isaac Lab provides a reliable way
    # For now, just verify the paths exist
    logger.info(f"Expected Isaac Lab {expected_lab_version}, Isaac Sim {expected_sim_version}")
    
    return True


def verify_nepher_installed() -> bool:
    """
    Verify the nepher (envhub) package is installed.
    
    Returns:
        True if nepher is installed
    """
    try:
        import nepher  # noqa: F401
        logger.info("nepher package is installed")
        return True
    except ImportError:
        logger.warning("nepher package is not installed")
        return False


def _ensure_name_symlink(cache_manager, original_name: str, resolved_name: str) -> None:
    """Create a symlink from the original env name to the resolved (cached) name.

    This allows ``nepher.load_env(original_name)`` to find the environment
    that was downloaded and cached under its resolved server-side ID.
    """
    link_path = cache_manager.get_env_cache_path(original_name)
    target_path = cache_manager.get_env_cache_path(resolved_name)

    if link_path.exists() or link_path.is_symlink():
        return

    try:
        link_path.symlink_to(target_path)
        logger.info(f"  Created cache symlink: {original_name} → {resolved_name}")
    except OSError as exc:
        logger.warning(f"  Could not create symlink {link_path} → {target_path}: {exc}")


async def download_environments(
    env_ids: List[str],
    cache_path: Optional[Path] = None,
    category: str = "navigation",
) -> None:
    """
    Download required environments using nepher (envhub).
    
    Args:
        env_ids: List of environment IDs to download
        cache_path: Optional custom cache path
        category: Environment category (default: "navigation")
    """
    try:
        from nepher.storage.cache import get_cache_manager
        from nepher.api.client import get_client
        from nepher.storage.bundle import BundleManager
        
        cache_manager = get_cache_manager(cache_dir=cache_path, category=category)
        client = get_client(api_url="https://envhub-api.nepher.ai")
        
        for env_id in env_ids:
            logger.info(f"Checking environment: {env_id}")
            
            # Check if already cached
            if cache_manager.is_cached(env_id):
                logger.info(f"  Already cached: {env_id}")
                continue
            
            # Resolve the actual env ID (the env_id in the config may be the
            # human-readable name rather than the server-side UUID).
            actual_env_id = env_id
            try:
                env_info = client.get_environment(env_id)
                actual_env_id = env_info.get("id", env_id)
            except Exception:
                logger.debug(f"  Could not fetch env by id, searching by name: {env_id}")
                envs = client.list_environments(category=category, search=env_id, limit=10)
                matches = [e for e in envs if e.get("original_name") == env_id]
                if not matches:
                    matches = [e for e in envs if env_id.lower() in e.get("original_name", "").lower()]
                if matches:
                    matches.sort(key=lambda x: x.get("uploaded_at", ""), reverse=True)
                    actual_env_id = matches[0].get("id", env_id)
            
            # Re-check cache with resolved ID (may differ from the name)
            if actual_env_id != env_id and cache_manager.is_cached(actual_env_id):
                logger.info(f"  Already cached (resolved {env_id} → {actual_env_id})")
                _ensure_name_symlink(cache_manager, env_id, actual_env_id)
                continue
            
            # Download environment bundle
            logger.info(f"  Downloading: {actual_env_id}")
            env_cache_path = cache_manager.get_env_cache_path(actual_env_id)
            zip_path = env_cache_path.parent / f"{actual_env_id}.zip"
            
            client.download_environment(actual_env_id, zip_path)
            
            # Extract bundle
            logger.info(f"  Extracting: {actual_env_id}")
            BundleManager.extract_bundle(zip_path, env_cache_path)
            
            # Clean up zip
            if zip_path.exists():
                zip_path.unlink()
            
            logger.info(f"  Downloaded and cached: {actual_env_id}")
            
            # Create symlink so load_env(env_id) can find it by original name
            if actual_env_id != env_id:
                _ensure_name_symlink(cache_manager, env_id, actual_env_id)
            
    except ImportError:
        logger.error("nepher package not installed - cannot download environments")
        raise SetupError("nepher package not installed")
    except Exception as e:
        logger.error(f"Failed to download environments: {e}")
        raise SetupError(f"Environment download failed: {e}")


async def setup_eval_repo(
    repo_url: str,
    target_path: Path,
) -> None:
    """
    Clone or update the evaluation repository.
    
    Args:
        repo_url: Git repository URL
        target_path: Path to clone/update to
    """
    if target_path.exists():
        # Update existing repo
        logger.info(f"Updating evaluation repo at {target_path}")
        return_code, stdout, stderr = await run_command_async(
            ["git", "-C", str(target_path), "pull"],
            timeout=120,
        )
        if return_code != 0:
            logger.warning(f"Git pull failed: {stderr}")
    else:
        # Clone new repo
        logger.info(f"Cloning evaluation repo to {target_path}")
        return_code, stdout, stderr = await run_command_async(
            ["git", "clone", repo_url, str(target_path)],
            timeout=300,
        )
        if return_code != 0:
            raise SetupError(f"Git clone failed: {stderr}")
    
    # Install the repo
    logger.info("Installing evaluation repo...")
    return_code, stdout, stderr = await run_command_async(
        [sys.executable, "-m", "pip", "install", "-e", str(target_path)],
        timeout=300,
    )
    if return_code != 0:
        raise SetupError(f"pip install failed: {stderr}")
    
    logger.info("Evaluation repo setup complete")


class SetupManager:
    """
    Manages validator setup phase.
    
    Handles all setup tasks including:
    - Verification of Isaac Lab/Sim
    - Downloading tournament configs
    - Downloading environments
    - Setting up evaluation repo
    """

    def __init__(self, config: ValidatorConfig, api: TournamentAPI):
        """
        Initialize setup manager.
        
        Args:
            config: Validator configuration
            api: Tournament API client
        """
        self.config = config
        self.api = api
        self._setup_complete = False

    @property
    def is_setup_complete(self) -> bool:
        """Check if setup has been completed."""
        return self._setup_complete

    async def run_setup(self, tournament_id: str) -> None:
        """
        Run complete setup phase.
        
        Args:
            tournament_id: Tournament ID to setup for
            
        Raises:
            SetupError: If any setup step fails
        """
        logger.info("=" * 60)
        logger.info("Starting validator setup phase")
        logger.info("=" * 60)
        
        # Step 1: Verify Isaac installation
        logger.info("Step 1: Verifying Isaac Lab/Sim installation...")
        if not verify_isaac_installation(
            self.config.isaac.lab_version,
            self.config.isaac.sim_version,
        ):
            raise SetupError("Isaac Lab/Sim verification failed")
        
        # Step 2: Verify nepher installed
        logger.info("Step 2: Verifying nepher package...")
        if not verify_nepher_installed():
            raise SetupError("nepher package not installed")
        
        # Step 3: Download tournament configs
        logger.info("Step 3: Downloading tournament configurations...")
        await self._download_configs(tournament_id)
        
        # Step 4: Download environments
        logger.info("Step 4: Downloading required environments...")
        env_ids = self._get_required_env_ids()
        await download_environments(env_ids, self.config.paths.env_cache)
        
        # Propagate the cache directory to the process environment so that
        # any subprocess (e.g. the evaluation script) using the nepher library
        # resolves the same cache location.
        os.environ["NEPHER_CACHE_DIR"] = str(self.config.paths.env_cache)
        
        # Step 5: Setup evaluation repo
        logger.info("Step 5: Setting up evaluation repository...")
        await setup_eval_repo(
            repo_url=self.config.paths.eval_repo_url,
            target_path=self.config.paths.eval_repo,
        )
        
        self._setup_complete = True
        logger.info("=" * 60)
        logger.info("Setup phase complete!")
        logger.info("=" * 60)

    async def _download_configs(self, tournament_id: str) -> None:
        """Download and save tournament configurations."""
        workspace = self.config.paths.workspace
        
        # Download subnet config
        logger.info("  Downloading subnet configuration...")
        subnet_config = await self.api.get_subnet_config(tournament_id)
        subnet_config_path = workspace / "subnet_config.yaml"
        save_yaml(subnet_config, subnet_config_path)
        
        # Download task config
        logger.info("  Downloading task configuration...")
        task_config = await self.api.get_task_config(tournament_id)
        task_config_path = workspace / "task_config.yaml"
        save_yaml(task_config, task_config_path)
        
        # Store task config for later use
        self._task_config = task_config

    def _get_required_env_ids(self) -> List[str]:
        """Get list of required environment IDs from task config."""
        if not hasattr(self, "_task_config"):
            raise SetupError("Task configuration not downloaded")
        
        env_scenes = self._task_config.get("env_scenes", [])
        return [scene["env_id"] for scene in env_scenes]

    def reset(self) -> None:
        """Reset setup state."""
        self._setup_complete = False
        self._task_config = None

