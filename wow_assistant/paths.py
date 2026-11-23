"""Resolve project data independently of the current working directory."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
CONFIG = ROOT / "config"
DIAGNOSTICS = ROOT / "diagnostics"


def config_path(name):
    legacy = ROOT / name
    return legacy if legacy.is_file() else CONFIG / name


def asset_path(name):
    legacy = ROOT / name
    return legacy if legacy.is_file() else ASSETS / name
