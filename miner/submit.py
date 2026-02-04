"""
Agent submission logic.

Handles:
- Agent structure validation
- ZIP archive creation
- Signature generation
- Upload to tournament backend
"""

import time
import tempfile
from pathlib import Path
from typing import Tuple, List

from nepher_core.api import TournamentAPI
from nepher_core.wallet import load_wallet, get_hotkey, get_public_key, sign_message, create_file_info
from nepher_core.utils.helpers import (
    compute_checksum,
    zip_directory,
    get_file_size,
)
from nepher_core.utils.logging import get_logger

logger = get_logger(__name__)


# Required files/directories for a valid agent
REQUIRED_STRUCTURE = {
    "best_policy": "directory",
    "best_policy/best_policy.pt": "file",
    "source": "directory",
}

# Optional but recommended files
RECOMMENDED_STRUCTURE = {
    "scripts/list_envs.py": "file",
    "scripts/rsl_rl/play.py": "file",
}


def validate_agent_structure(agent_path: Path) -> Tuple[bool, List[str]]:
    """
    Validate the agent directory structure.
    
    Args:
        agent_path: Path to agent directory
        
    Returns:
        Tuple of (is_valid, list of errors)
    """
    errors = []
    
    if not agent_path.exists():
        return False, [f"Agent path does not exist: {agent_path}"]
    
    if not agent_path.is_dir():
        return False, [f"Agent path is not a directory: {agent_path}"]
    
    # Check required structure
    for rel_path, item_type in REQUIRED_STRUCTURE.items():
        full_path = agent_path / rel_path
        
        if not full_path.exists():
            errors.append(f"Required {item_type} missing: {rel_path}")
        elif item_type == "directory" and not full_path.is_dir():
            errors.append(f"Expected directory but found file: {rel_path}")
        elif item_type == "file" and not full_path.is_file():
            errors.append(f"Expected file but found directory: {rel_path}")
    
    # Check for source/<task_module> directory
    source_dir = agent_path / "source"
    if source_dir.exists() and source_dir.is_dir():
        subdirs = [d for d in source_dir.iterdir() if d.is_dir()]
        if not subdirs:
            errors.append("source/ directory must contain at least one task module")
        else:
            # Check for __init__.py in task module
            for subdir in subdirs:
                if not (subdir / "__init__.py").exists():
                    errors.append(f"Task module missing __init__.py: source/{subdir.name}/")
    
    # Warn about missing recommended files
    for rel_path, item_type in RECOMMENDED_STRUCTURE.items():
        full_path = agent_path / rel_path
        if not full_path.exists():
            logger.warning(f"Recommended {item_type} missing: {rel_path}")
    
    return len(errors) == 0, errors


async def submit_agent(
    agent_path: Path,
    wallet_name: str,
    wallet_hotkey: str,
    api_key: str,
    api_url: str,
) -> str:
    """
    Submit an agent to the tournament.
    
    The active tournament is automatically selected by the backend.
    
    Args:
        agent_path: Path to agent directory
        wallet_name: Wallet name
        wallet_hotkey: Hotkey name
        api_key: Tournament API key
        api_url: Tournament API URL
        
    Returns:
        Agent ID of the submitted agent
        
    Raises:
        ValueError: If agent validation fails
        APIError: If API request fails
    """
    # Load wallet
    wallet = load_wallet(name=wallet_name, hotkey=wallet_hotkey)
    miner_hotkey = get_hotkey(wallet)
    public_key = get_public_key(wallet)
    
    # Create ZIP archive first (need checksum for signing)
    logger.info("Creating submission archive...")
    with tempfile.TemporaryDirectory() as temp_dir:
        archive_path = Path(temp_dir) / "agent.zip"
        zip_directory(agent_path, archive_path)
        
        # Compute checksum and size
        content_hash = compute_checksum(archive_path)
        file_size = get_file_size(archive_path)
        logger.info(f"Archive: {file_size} bytes, checksum: {content_hash[:16]}...")
        
        # Create file_info and sign it
        timestamp = int(time.time())
        file_info = create_file_info(miner_hotkey, content_hash, timestamp)
        signature = sign_message(wallet, file_info)
        logger.debug(f"Signed file_info with hotkey: {miner_hotkey}")
        
        # Create API client
        async with TournamentAPI(api_key=api_key, base_url=api_url) as api:
            # Request upload token (this also validates and gets tournament)
            logger.info("Requesting upload token...")
            token = await api.request_upload_token(
                miner_hotkey=miner_hotkey,
                public_key=public_key,
                file_info=file_info,
                signature=signature,
                file_size=file_size,
            )
            
            tournament_id = token.tournament_id
            logger.info(f"Using tournament: {tournament_id}")
            
            # Upload agent
            logger.info("Uploading agent...")
            agent = await api.upload_agent(
                tournament_id=tournament_id,
                upload_token=token.upload_token,
                miner_hotkey=miner_hotkey,
                content_hash=content_hash,
                file_path=archive_path,
            )
            
            logger.info(f"✅ Agent submitted successfully!")
            logger.info(f"   Agent ID: {agent.id}")
            logger.info(f"   Tournament: {tournament_id}")
            logger.info(f"   Miner Hotkey: {miner_hotkey}")
            
            return agent.id

