"""Builders of a figure release tarball and of the repository tree the generator writes into."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
from pathlib import Path

from scripts.generate_figure_vocabulary import (
    MODULE_PATH,
    PIN_PATH,
    VOCABULARY_MEMBER,
    Pin,
    write_pin,
)

RELEASE_URL = "https://releases.example/figure-9.9.9.tgz"


def vocabulary_document(**fields: object) -> dict[str, object]:
    return {"version": 1, "gestures": ["wave", "nod"], "moods": ["happy", "sad_2"]} | fields


def tarball(
    vocabulary: bytes | dict[str, object] | None = None, *, member: str = VOCABULARY_MEMBER
) -> bytes:
    content = (
        vocabulary
        if isinstance(vocabulary, bytes)
        else json.dumps(vocabulary_document() if vocabulary is None else vocabulary).encode()
    )
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        entry = tarfile.TarInfo(member)
        entry.size = len(content)
        archive.addfile(entry, io.BytesIO(content))
    return buffer.getvalue()


def directory_tarball(*, member: str = VOCABULARY_MEMBER) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        entry = tarfile.TarInfo(member)
        entry.type = tarfile.DIRTYPE
        archive.addfile(entry)
    return buffer.getvalue()


def integrity_of(content: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(content).digest()).decode("ascii")


def pin_for(content: bytes) -> Pin:
    return Pin(RELEASE_URL, integrity_of(content))


def repository(root: Path, pin: Pin) -> Path:
    """A tree holding the pin file and the directory the generated module goes into."""
    (root / PIN_PATH).parent.mkdir(parents=True)
    write_pin(root / PIN_PATH, pin)
    (root / MODULE_PATH).parent.mkdir(parents=True)
    return root
