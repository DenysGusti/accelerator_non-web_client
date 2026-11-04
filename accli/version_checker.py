import os
import re
import sys
import urllib.request
import json
from ._version import VERSION

RELEASES_URL = "https://github.com/iiasa/accelerator_non-web_client/releases"

def is_standalone_binary() -> bool:
    """Returns True if running as a standalone binary (PyApp or PyInstaller executable)."""
    return getattr(sys, "frozen", False) or bool(os.environ.get("PYAPP"))

def get_upgrade_instruction() -> str:
    """Returns the appropriate upgrade instruction based on whether running from an executable or pip."""
    if is_standalone_binary():
        return f"Download the latest release from: {RELEASES_URL}"
    return "To update, run: pip install -U accli"

def parse_version(ver_str: str):
    """
    Parse a version string (e.g. 'v2.0.15' or '2.0.14') into a tuple of ints
    ignoring leading 'v'. Extra alphanumeric parts are preserved for comparison.
    """
    clean_str = ver_str.lstrip("v").strip()
    parts = []
    for item in re.split(r'[-.+_]', clean_str):
        if item.isdigit():
            parts.append(int(item))
        else:
            parts.append(item)
    return tuple(parts)

def check_for_updates(package_name: str = "accli", current_version: str = VERSION, timeout: float = 2.0):
    """
    Check PyPI for a newer version of the package directly (no caching).
    Returns:
        (is_update_available, latest_version)
    Never raises an exception, silently fails if offline or timed out.
    """
    try:
        url = f"https://pypi.org/pypi/{package_name}/json"
        req = urllib.request.Request(url, headers={"User-Agent": f"{package_name}-client/{current_version}"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 200:
                data = json.loads(response.read().decode("utf-8"))
                latest_ver_str = data.get("info", {}).get("version")
                if latest_ver_str:
                    curr_parsed = parse_version(current_version)
                    latest_parsed = parse_version(latest_ver_str)
                    if latest_parsed > curr_parsed:
                        return True, latest_ver_str
    except Exception:
        pass
    return False, None
