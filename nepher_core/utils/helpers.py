"""
Common helper utilities.

Provides reusable functions for:
- File operations (checksums, compression)
- Module management
- Command execution
"""

import asyncio
import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Optional, List

from nepher_core.utils.logging import get_logger

logger = get_logger(__name__)

# Directories/files to exclude when creating submission archives
ARCHIVE_EXCLUDES = [
    "__pycache__",
    ".git",
    ".gitignore",
    "*.pyc",
    "*.pyo",
    "*.egg-info",
    "logs",
    "outputs",
    ".env",
    "venv",
    ".venv",
    "node_modules",
]


def compute_checksum(file_path: Path, algorithm: str = "sha256") -> str:
    """
    Compute checksum of a file.
    
    Args:
        file_path: Path to the file
        algorithm: Hash algorithm (sha256, md5, etc.)
        
    Returns:
        Hex-encoded checksum
    """
    hash_func = hashlib.new(algorithm)
    
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hash_func.update(chunk)
    
    return hash_func.hexdigest()


def _should_exclude(path: Path, excludes: List[str]) -> bool:
    """Check if a path should be excluded from archive."""
    name = path.name
    
    for pattern in excludes:
        if pattern.startswith("*"):
            if name.endswith(pattern[1:]):
                return True
        elif name == pattern:
            return True
    
    return False


def zip_directory(
    source_dir: Path,
    output_path: Path,
    excludes: Optional[List[str]] = None,
) -> Path:
    """
    Create a ZIP archive of a directory.
    
    Args:
        source_dir: Directory to archive
        output_path: Output ZIP file path
        excludes: Patterns to exclude (uses ARCHIVE_EXCLUDES if None)
        
    Returns:
        Path to created archive
    """
    if excludes is None:
        excludes = ARCHIVE_EXCLUDES
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in source_dir.rglob("*"):
            if file_path.is_file():
                # Check exclusions
                skip = False
                for part in file_path.relative_to(source_dir).parts:
                    if _should_exclude(Path(part), excludes):
                        skip = True
                        break
                
                if not skip:
                    arcname = file_path.relative_to(source_dir)
                    zf.write(file_path, arcname)
    
    logger.info(f"Created archive: {output_path}")
    return output_path


def unzip_file(
    archive_path: Path,
    output_dir: Path,
) -> Path:
    """
    Extract a ZIP archive.
    
    Args:
        archive_path: Path to ZIP file
        output_dir: Directory to extract to
        
    Returns:
        Path to extraction directory
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    
    with zipfile.ZipFile(archive_path, "r") as zf:
        zf.extractall(output_dir)
    
    logger.info(f"Extracted archive to: {output_dir}")
    return output_dir


def is_module_installed(module_name: str) -> bool:
    """
    Check if a Python module is installed.
    
    Args:
        module_name: Module name to check
        
    Returns:
        True if module is installed
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", module_name],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except Exception:
        return False


def run_command(
    command: List[str],
    cwd: Optional[Path] = None,
    timeout: Optional[float] = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """
    Run a shell command synchronously.
    
    Args:
        command: Command and arguments
        cwd: Working directory
        timeout: Timeout in seconds
        check: Raise exception on non-zero exit code
        
    Returns:
        CompletedProcess instance
        
    Raises:
        subprocess.CalledProcessError: If check=True and command fails
        subprocess.TimeoutExpired: If timeout is exceeded
    """
    logger.debug(f"Running command: {' '.join(command)}")
    
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    
    if check and result.returncode != 0:
        logger.error(f"Command failed: {result.stderr}")
        result.check_returncode()
    
    return result


async def run_command_async(
    command: List[str],
    cwd: Optional[Path] = None,
    timeout: Optional[float] = None,
) -> tuple[int, str, str]:
    """
    Run a shell command asynchronously.
    
    Args:
        command: Command and arguments
        cwd: Working directory
        timeout: Timeout in seconds
        
    Returns:
        Tuple of (return_code, stdout, stderr)
    """
    logger.debug(f"Running async command: {' '.join(command)}")
    
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout,
        )
        return (
            process.returncode or 0,
            stdout.decode() if stdout else "",
            stderr.decode() if stderr else "",
        )
    except asyncio.TimeoutError:
        process.kill()
        raise


def clean_directory(path: Path) -> None:
    """
    Remove a directory and all its contents.
    
    Args:
        path: Directory to remove
    """
    if path.exists():
        shutil.rmtree(path)
        logger.debug(f"Cleaned directory: {path}")


def ensure_directory(path: Path) -> Path:
    """
    Ensure a directory exists, creating it if necessary.
    
    Args:
        path: Directory path
        
    Returns:
        The path
    """
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_file_size(path: Path) -> int:
    """
    Get the size of a file in bytes.
    
    Args:
        path: File path
        
    Returns:
        File size in bytes
    """
    return path.stat().st_size

