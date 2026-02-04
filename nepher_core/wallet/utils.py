"""
Bittensor wallet utilities.

Provides helper functions for wallet operations including:
- Loading wallets
- Signing messages
- Verifying signatures
"""

from pathlib import Path
from typing import Optional

import bittensor as bt
from bittensor_wallet import Wallet

from nepher_core.utils.logging import get_logger

logger = get_logger(__name__)


def load_wallet(
    name: str = "default",
    hotkey: str = "default",
    path: Optional[str] = None,
) -> Wallet:
    """
    Load a Bittensor wallet.
    
    Args:
        name: Wallet name
        hotkey: Hotkey name
        path: Custom wallet path (uses default if None)
        
    Returns:
        Loaded wallet instance
        
    Raises:
        ValueError: If wallet or hotkey doesn't exist
    """
    wallet = Wallet(
        name=name,
        hotkey=hotkey,
        path=path,
    )
    
    # Verify wallet exists
    if not wallet.coldkey_file.exists_on_device():
        raise ValueError(f"Wallet '{name}' not found at {wallet.path}")
    
    if not wallet.hotkey_file.exists_on_device():
        raise ValueError(f"Hotkey '{hotkey}' not found for wallet '{name}'")
    
    logger.debug(f"Loaded wallet: name={name}, hotkey={hotkey}")
    return wallet


def get_hotkey(wallet: Wallet) -> str:
    """
    Get the SS58 address of the wallet's hotkey.
    
    Args:
        wallet: Loaded wallet
        
    Returns:
        SS58 hotkey address
    """
    return wallet.hotkey.ss58_address


def get_public_key(wallet: Wallet) -> str:
    """
    Get the hex-encoded public key of the wallet's hotkey.
    
    Args:
        wallet: Loaded wallet
        
    Returns:
        Hex-encoded public key
    """
    return wallet.hotkey.public_key.hex()


def sign_message(wallet: Wallet, message: str) -> str:
    """
    Sign a message using the wallet's hotkey.
    
    Args:
        wallet: Loaded wallet
        message: Message to sign
        
    Returns:
        Hex-encoded signature
    """
    signature = wallet.hotkey.sign(message.encode())
    return signature.hex()


def verify_signature(
    hotkey_ss58: str,
    message: str,
    signature: str,
) -> bool:
    """
    Verify a signature against a hotkey.
    
    Args:
        hotkey_ss58: SS58 address of the signer
        message: Original message
        signature: Hex-encoded signature
        
    Returns:
        True if signature is valid
    """
    try:
        from substrateinterface import Keypair
        
        keypair = Keypair(ss58_address=hotkey_ss58)
        sig_bytes = bytes.fromhex(signature)
        
        return keypair.verify(message.encode(), sig_bytes)
    except Exception as e:
        logger.warning(f"Signature verification failed: {e}")
        return False


def create_file_info(
    miner_hotkey: str,
    content_hash: str,
    timestamp: int,
) -> str:
    """
    Create file_info string for signing agent submissions.
    
    Format: "hotkey:content_hash:timestamp"
    
    Args:
        miner_hotkey: Miner's SS58 hotkey address
        content_hash: SHA256 checksum of the file
        timestamp: Unix timestamp
        
    Returns:
        Formatted file_info for signing
    """
    return f"{miner_hotkey}:{content_hash}:{timestamp}"


def get_subtensor(network: str = "finney") -> bt.Subtensor:
    """
    Get a Bittensor subtensor connection.
    
    Args:
        network: Network name (finney, test, local)
        
    Returns:
        Connected subtensor instance
    """
    logger.info(f"Connecting to Bittensor network: {network}")
    return bt.Subtensor(network=network)


def get_metagraph(
    subtensor: bt.Subtensor,
    netuid: int,
) -> bt.Metagraph:
    """
    Get the metagraph for a subnet.
    
    Args:
        subtensor: Connected subtensor
        netuid: Subnet UID
        
    Returns:
        Metagraph for the subnet
    """
    logger.debug(f"Loading metagraph for netuid={netuid}")
    return subtensor.metagraph(netuid=netuid)


def find_uid_for_hotkey(
    metagraph: bt.Metagraph,
    hotkey: str,
) -> Optional[int]:
    """
    Find the UID for a hotkey in the metagraph.
    
    Args:
        metagraph: Subnet metagraph
        hotkey: SS58 hotkey address
        
    Returns:
        UID if found, None otherwise
    """
    try:
        return metagraph.hotkeys.index(hotkey)
    except ValueError:
        return None

