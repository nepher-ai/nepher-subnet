"""Tests for helper utilities."""

import os
import pytest
import tempfile
from pathlib import Path

from nepher_core.utils.helpers import (
    compute_checksum,
    zip_directory,
    unzip_file,
    clean_directory,
    ensure_directory,
    get_file_size,
)


class TestChecksums:
    """Test checksum computation."""

    def test_compute_checksum_sha256(self):
        """Test SHA256 checksum computation."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write("test content")
            f.flush()
            
            try:
                checksum = compute_checksum(Path(f.name))
                assert len(checksum) == 64  # SHA256 hex length
                assert checksum.isalnum()
            finally:
                os.unlink(f.name)

    def test_compute_checksum_md5(self):
        """Test MD5 checksum computation."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            f.write("test content")
            f.flush()
            
            try:
                checksum = compute_checksum(Path(f.name), algorithm="md5")
                assert len(checksum) == 32  # MD5 hex length
            finally:
                os.unlink(f.name)

    def test_same_content_same_checksum(self):
        """Test that same content produces same checksum."""
        content = "identical content"
        
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f1:
            f1.write(content)
            f1.flush()
            
            with tempfile.NamedTemporaryFile(mode="w", delete=False) as f2:
                f2.write(content)
                f2.flush()
                
                try:
                    checksum1 = compute_checksum(Path(f1.name))
                    checksum2 = compute_checksum(Path(f2.name))
                    assert checksum1 == checksum2
                finally:
                    os.unlink(f1.name)
                    os.unlink(f2.name)


class TestZipOperations:
    """Test ZIP operations."""

    def test_zip_and_unzip(self):
        """Test zipping and unzipping a directory."""
        with tempfile.TemporaryDirectory() as source_dir:
            with tempfile.TemporaryDirectory() as output_dir:
                source_path = Path(source_dir)
                output_path = Path(output_dir)
                
                # Create test files
                (source_path / "file1.txt").write_text("content 1")
                (source_path / "subdir").mkdir()
                (source_path / "subdir" / "file2.txt").write_text("content 2")
                
                # Zip
                archive_path = output_path / "test.zip"
                zip_directory(source_path, archive_path)
                
                assert archive_path.exists()
                assert archive_path.stat().st_size > 0
                
                # Unzip
                extract_path = output_path / "extracted"
                unzip_file(archive_path, extract_path)
                
                # Verify contents
                assert (extract_path / "file1.txt").read_text() == "content 1"
                assert (extract_path / "subdir" / "file2.txt").read_text() == "content 2"

    def test_zip_excludes_patterns(self):
        """Test that exclusion patterns work."""
        with tempfile.TemporaryDirectory() as source_dir:
            with tempfile.TemporaryDirectory() as output_dir:
                source_path = Path(source_dir)
                
                # Create test files including ones to exclude
                (source_path / "keep.txt").write_text("keep")
                (source_path / "__pycache__").mkdir()
                (source_path / "__pycache__" / "cache.pyc").write_text("cache")
                (source_path / "test.pyc").write_text("pyc")
                
                # Zip with exclusions
                archive_path = Path(output_dir) / "test.zip"
                zip_directory(source_path, archive_path)
                
                # Unzip and verify exclusions
                extract_path = Path(output_dir) / "extracted"
                unzip_file(archive_path, extract_path)
                
                assert (extract_path / "keep.txt").exists()
                assert not (extract_path / "__pycache__").exists()
                assert not (extract_path / "test.pyc").exists()


class TestDirectoryOperations:
    """Test directory operations."""

    def test_clean_directory(self):
        """Test directory cleaning."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "to_clean"
            path.mkdir()
            (path / "file.txt").write_text("content")
            (path / "subdir").mkdir()
            (path / "subdir" / "nested.txt").write_text("nested")
            
            clean_directory(path)
            
            assert not path.exists()

    def test_clean_nonexistent_directory(self):
        """Test cleaning non-existent directory doesn't error."""
        path = Path("/nonexistent/path/to/clean")
        clean_directory(path)  # Should not raise

    def test_ensure_directory(self):
        """Test directory creation."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "new" / "nested" / "dir"
            
            result = ensure_directory(path)
            
            assert path.exists()
            assert path.is_dir()
            assert result == path

    def test_get_file_size(self):
        """Test file size calculation."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            content = "x" * 1000
            f.write(content)
            f.flush()
            
            try:
                size = get_file_size(Path(f.name))
                assert size == 1000
            finally:
                os.unlink(f.name)

