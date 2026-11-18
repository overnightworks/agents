# ruff: noqa: T201 - the command line's one output owner; it prints each forbidden package nothing provides
"""Fails when `.importlinter` forbids a package that nothing provides.

A forbidden import of a misspelled or renamed package never fires, so the contract guards nothing.
A forbidden package is real when a distribution of the environment or of `uv.lock` provides it, or
when it is a real package `agents` keeps out of its tree.
"""

from __future__ import annotations

import configparser
import re
import sys
import tomllib
from collections.abc import Iterable
from importlib.metadata import packages_distributions
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parent.parent
CONTRACTS_PATH: Final = Path(".importlinter")
LOCK_PATH: Final = Path("uv.lock")
OWN_PACKAGE: Final = "agents"
# Real packages that `agents` neither installs nor locks: the import packages of the hosts and of
# the speech client, from their manifests, and httpx, which httpx2 replaces. They are named once,
# so every contract is held to these spellings.
UNINSTALLED_PACKAGES: Final = frozenset(
    {
        "gitmirror",
        "hal",
        "hopin",
        "httpx",
        "personas",
        "presentator",
        "skat",
        "songmaker_cli",
        "speech_client",
        "webauth",
    }
)


def forbidden_packages(contracts_text: str) -> frozenset[str]:
    """The top-level package of every external module a contract forbids."""
    contracts = configparser.ConfigParser()
    contracts.read_string(contracts_text)
    names = (
        name
        for section in contracts.sections()
        for name in contracts.get(section, "forbidden_modules", fallback="").split()
    )
    return frozenset(name.split(".")[0] for name in names) - {OWN_PACKAGE}


def locked_modules(lock_text: str) -> frozenset[str]:
    """The locked distribution names spelled as import names."""
    packages = tomllib.loads(lock_text).get("package", [])
    return frozenset(re.sub(r"[-.]+", "_", package["name"]).lower() for package in packages)


def unprovided_packages(
    forbidden: Iterable[str], provided_modules: Iterable[str]
) -> frozenset[str]:
    """The forbidden packages that no distribution provides and `agents` does not keep out."""
    return frozenset(forbidden) - frozenset(provided_modules) - UNINSTALLED_PACKAGES


def main(*, root: Path) -> int:
    provided = set(packages_distributions()) | locked_modules(
        (root / LOCK_PATH).read_text(encoding="utf-8")
    )
    missing = unprovided_packages(
        forbidden_packages((root / CONTRACTS_PATH).read_text(encoding="utf-8")), provided
    )
    for name in sorted(missing):
        print(f"{CONTRACTS_PATH} forbids {name}, which no distribution provides", file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main(root=ROOT))
