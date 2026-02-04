"""Tests for configuration loading."""

import os
import pytest
import tempfile
from pathlib import Path

from nepher_core.config.loader import load_yaml, save_yaml, load_config, ConfigManager
from nepher_core.config.models import (
    ValidatorConfig,
    MinerConfig,
    SubnetConfig,
    WalletConfig,
    resolve_env_vars,
)


class TestEnvVarResolution:
    """Test environment variable resolution."""

    def test_resolve_simple_var(self):
        """Test resolving simple environment variable."""
        os.environ["TEST_VAR"] = "test_value"
        try:
            result = resolve_env_vars("${TEST_VAR}")
            assert result == "test_value"
        finally:
            del os.environ["TEST_VAR"]

    def test_resolve_with_default(self):
        """Test resolving variable with default."""
        # Variable not set - use default
        result = resolve_env_vars("${UNSET_VAR:-default_value}")
        assert result == "default_value"
        
        # Variable set - use value
        os.environ["SET_VAR"] = "actual_value"
        try:
            result = resolve_env_vars("${SET_VAR:-default_value}")
            assert result == "actual_value"
        finally:
            del os.environ["SET_VAR"]

    def test_resolve_missing_required(self):
        """Test error on missing required variable."""
        with pytest.raises(ValueError) as exc_info:
            resolve_env_vars("${MISSING_REQUIRED_VAR}")
        
        assert "MISSING_REQUIRED_VAR" in str(exc_info.value)

    def test_no_var_passthrough(self):
        """Test that non-variable strings pass through unchanged."""
        result = resolve_env_vars("regular_string")
        assert result == "regular_string"


class TestYamlLoader:
    """Test YAML loading and saving."""

    def test_load_yaml(self):
        """Test loading YAML file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("""
subnet:
  network: finney
  subnet_uid: 49
wallet:
  name: test_wallet
""")
            f.flush()
            
            try:
                data = load_yaml(Path(f.name))
                assert data["subnet"]["network"] == "finney"
                assert data["subnet"]["subnet_uid"] == 49
                assert data["wallet"]["name"] == "test_wallet"
            finally:
                os.unlink(f.name)

    def test_load_yaml_with_env_vars(self):
        """Test loading YAML with environment variables."""
        os.environ["TEST_API_KEY"] = "secret_key"
        
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("""
tournament:
  api_key: "${TEST_API_KEY}"
  api_url: "https://api.test.com"
""")
            f.flush()
            
            try:
                data = load_yaml(Path(f.name))
                assert data["tournament"]["api_key"] == "secret_key"
                assert data["tournament"]["api_url"] == "https://api.test.com"
            finally:
                os.unlink(f.name)
                del os.environ["TEST_API_KEY"]

    def test_save_yaml(self):
        """Test saving YAML file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "test.yaml"
            
            data = {"key": "value", "nested": {"a": 1, "b": 2}}
            save_yaml(data, path)
            
            # Verify file was created and can be loaded
            loaded = load_yaml(path)
            assert loaded == data

    def test_load_yaml_file_not_found(self):
        """Test error on missing file."""
        with pytest.raises(FileNotFoundError):
            load_yaml(Path("/nonexistent/file.yaml"))


class TestConfigModels:
    """Test configuration models."""

    def test_subnet_config_defaults(self):
        """Test SubnetConfig defaults."""
        config = SubnetConfig()
        assert config.network == "finney"
        assert config.subnet_uid == 49

    def test_subnet_config_validation(self):
        """Test SubnetConfig validation."""
        with pytest.raises(ValueError):
            SubnetConfig(network="invalid_network")

    def test_wallet_config_env_resolution(self):
        """Test WalletConfig resolves env vars."""
        os.environ["TEST_WALLET"] = "my_wallet"
        try:
            config = WalletConfig(name="${TEST_WALLET}", hotkey="default")
            assert config.name == "my_wallet"
        finally:
            del os.environ["TEST_WALLET"]


class TestConfigManager:
    """Test ConfigManager."""

    def test_load_validator_config(self):
        """Test loading validator configuration."""
        os.environ["TEST_API_KEY"] = "test_key"
        
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("""
subnet:
  network: finney
  subnet_uid: 49
tournament:
  api_url: "https://api.test.com"
  api_key: "${TEST_API_KEY}"
wallet:
  name: validator
  hotkey: default
""")
            f.flush()
            
            try:
                manager = ConfigManager(Path(f.name))
                config = manager.load_validator_config()
                
                assert config.subnet.network == "finney"
                assert config.tournament.api_key == "test_key"
                assert config.wallet.name == "validator"
            finally:
                os.unlink(f.name)
                del os.environ["TEST_API_KEY"]

    def test_load_miner_config(self):
        """Test loading miner configuration."""
        os.environ["TEST_API_KEY"] = "miner_key"
        
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("""
tournament:
  api_url: "https://api.test.com"
  api_key: "${TEST_API_KEY}"
wallet:
  name: miner
  hotkey: default
""")
            f.flush()
            
            try:
                manager = ConfigManager(Path(f.name))
                config = manager.load_miner_config()
                
                assert config.tournament.api_key == "miner_key"
                assert config.wallet.name == "miner"
            finally:
                os.unlink(f.name)
                del os.environ["TEST_API_KEY"]

