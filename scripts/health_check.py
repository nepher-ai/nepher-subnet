#!/usr/bin/env python3
"""
Nepher Subnet Health Check Script.

Verifies that all components are properly installed and configured.
"""

import sys
import os
from pathlib import Path


def check_python_version():
    """Check Python version."""
    print("Checking Python version...", end=" ")
    version = sys.version_info
    if version.major >= 3 and version.minor >= 10:
        print(f"✅ Python {version.major}.{version.minor}.{version.micro}")
        return True
    else:
        print(f"❌ Python {version.major}.{version.minor} (need 3.10+)")
        return False


def check_nepher_core():
    """Check nepher_core package."""
    print("Checking nepher_core...", end=" ")
    try:
        import nepher_core
        print(f"✅ Version {nepher_core.__version__}")
        return True
    except ImportError as e:
        print(f"❌ Not installed ({e})")
        return False


def check_bittensor():
    """Check bittensor package."""
    print("Checking bittensor...", end=" ")
    try:
        import bittensor as bt
        print(f"✅ Installed")
        return True
    except ImportError as e:
        print(f"❌ Not installed ({e})")
        return False


def check_nepher_envhub():
    """Check nepher (envhub) package."""
    print("Checking nepher (envhub)...", end=" ")
    try:
        import nepher
        print(f"✅ Installed")
        return True
    except ImportError:
        print("⚠️ Not installed (required for validator)")
        return False


def check_isaac_lab():
    """Check Isaac Lab installation."""
    print("Checking Isaac Lab...", end=" ")
    isaaclab_path = os.environ.get("ISAACLAB_PATH")
    if isaaclab_path and Path(isaaclab_path).exists():
        print(f"✅ Found at {isaaclab_path}")
        return True
    else:
        print("⚠️ ISAACLAB_PATH not set (required for validator)")
        return False


def check_api_key():
    """Check API key configuration."""
    print("Checking API key...", end=" ")
    api_key = os.environ.get("NEPHER_API_KEY")
    if api_key:
        print(f"✅ Set ({api_key[:8]}...)")
        return True
    else:
        print("⚠️ NEPHER_API_KEY not set")
        return False


def check_wallet():
    """Check Bittensor wallet."""
    print("Checking wallet...", end=" ")
    try:
        from bittensor_wallet import Wallet
        
        wallet_name = os.environ.get("WALLET_NAME", "validator")
        hotkey_name = os.environ.get("WALLET_HOTKEY", "default")
        
        wallet = Wallet(name=wallet_name, hotkey=hotkey_name)
        
        if wallet.coldkey_file.exists_on_device():
            if wallet.hotkey_file.exists_on_device():
                print(f"✅ Found {wallet_name}/{hotkey_name}")
                return True
            else:
                print(f"❌ Hotkey '{hotkey_name}' not found")
                return False
        else:
            print(f"❌ Wallet '{wallet_name}' not found")
            return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False


def main():
    """Run all health checks."""
    print("=" * 50)
    print("Nepher Subnet Health Check")
    print("=" * 50)
    print()
    
    checks = [
        ("Python Version", check_python_version),
        ("Nepher Core", check_nepher_core),
        ("Bittensor", check_bittensor),
        ("Nepher EnvHub", check_nepher_envhub),
        ("Isaac Lab", check_isaac_lab),
        ("API Key", check_api_key),
        ("Wallet", check_wallet),
    ]
    
    results = []
    for name, check_func in checks:
        try:
            results.append(check_func())
        except Exception as e:
            print(f"❌ Error in {name}: {e}")
            results.append(False)
    
    print()
    print("=" * 50)
    passed = sum(results)
    total = len(results)
    
    if all(results):
        print(f"✅ All checks passed ({passed}/{total})")
        return 0
    else:
        print(f"⚠️ Some checks failed ({passed}/{total})")
        return 1


if __name__ == "__main__":
    sys.exit(main())

