"""
Common helper utilities.

Provides reusable functions for:
- File operations (checksums, compression)
- Module management
- Command execution
"""

import asyncio
import hashlib
import os
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
    max_total_bytes: Optional[int] = None,
) -> Path:
    """
    Create a ZIP archive of a directory.

    Symlinks are skipped (never followed) and nothing outside *source_dir*
    is archived.

    Args:
        source_dir: Directory to archive
        output_path: Output ZIP file path
        excludes: Patterns to exclude (uses ARCHIVE_EXCLUDES if None)
        max_total_bytes: Raise ValueError if archived files exceed this size

    Raises:
        ValueError: If max_total_bytes is exceeded
        
    Returns:
        Path to created archive
    """
    if excludes is None:
        excludes = ARCHIVE_EXCLUDES

    output_path.parent.mkdir(parents=True, exist_ok=True)
    root = source_dir.resolve()
    total_bytes = 0

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            current = Path(dirpath)
            # Symlinked directories are listed in dirnames but never descended
            # into (followlinks=False); drop excluded dirs in place.
            dirnames[:] = [
                d for d in dirnames
                if not _should_exclude(Path(d), excludes) and not (current / d).is_symlink()
            ]
            for name in filenames:
                file_path = current / name
                if _should_exclude(file_path, excludes):
                    continue
                # Never follow symlinks: the source tree may be attacker-controlled.
                if file_path.is_symlink() or not file_path.is_file():
                    continue
                if not file_path.resolve().is_relative_to(root):
                    continue
                if max_total_bytes is not None:
                    total_bytes += file_path.stat().st_size
                    if total_bytes > max_total_bytes:
                        raise ValueError(
                            f"Archive source exceeds {max_total_bytes} bytes: {source_dir}"
                        )
                zf.write(file_path, file_path.relative_to(root))

    logger.info(f"Created archive: {output_path}")
    return output_path


DEFAULT_MAX_UNZIP_BYTES = 8 * 1024**3  # 8 GB uncompressed
DEFAULT_MAX_UNZIP_ENTRIES = 50_000
MAX_COMPRESSION_RATIO = 200


def unzip_file(
    archive_path: Path,
    output_dir: Path,
    max_total_bytes: int = DEFAULT_MAX_UNZIP_BYTES,
    max_entries: int = DEFAULT_MAX_UNZIP_ENTRIES,
) -> Path:
    """
    Extract a ZIP archive safely.

    Validates every member path to prevent ZIP slip (path traversal) attacks,
    rejects symlinks that could escape the output directory, and rejects
    archives whose declared uncompressed size, entry count, or compression
    ratio indicate a zip bomb.

    Args:
        archive_path: Path to ZIP file
        output_dir: Directory to extract to
        max_total_bytes: Maximum total uncompressed size
        max_entries: Maximum number of archive members

    Returns:
        Path to extraction directory

    Raises:
        ValueError: If the archive is unsafe or exceeds the limits.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    resolved_output = output_dir.resolve()

    with zipfile.ZipFile(archive_path, "r") as zf:
        infos = zf.infolist()
        if len(infos) > max_entries:
            raise ValueError(f"Archive has too many entries ({len(infos)} > {max_entries})")
        total_size = sum(i.file_size for i in infos)
        if total_size > max_total_bytes:
            raise ValueError(
                f"Archive uncompressed size {total_size} exceeds limit {max_total_bytes}"
            )
        compressed = max(1, sum(i.compress_size for i in infos))
        if total_size > 100 * 1024**2 and total_size / compressed > MAX_COMPRESSION_RATIO:
            raise ValueError("Archive compression ratio is suspicious (possible zip bomb)")

        for info in infos:
            # Reject entries with absolute paths or parent-directory traversal
            member_path = (output_dir / info.filename).resolve()
            if not member_path.is_relative_to(resolved_output):
                raise ValueError(
                    f"Zip slip detected: '{info.filename}' escapes target directory"
                )
            # Reject symlinks (external_attr high byte 0xA0 = symlink on Unix)
            if (info.external_attr >> 16) & 0xF000 == 0xA000:
                raise ValueError(
                    f"Symlink in archive rejected: '{info.filename}'"
                )

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

