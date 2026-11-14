# ruff: noqa: T201 - the command line's one output owner; it prints what it wrote
"""Generates ``src/agents/figure_vocabulary.py`` from figure's pinned release.

figure owns the gestures and moods an agent can show. This script reads the one file
``package/dist/figure-vocabulary.json`` from the release tarball that ``figure_vocabulary_pin.toml``
pins by url and npm integrity, and writes the enums of the committed module. With
``--pin <url> <integrity>`` it first fetches that release and, once the integrity holds, raises
the pin. The output is deterministic, so CI regenerates it and requires an empty diff.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
import sys
import tarfile
import tomllib
import urllib.request
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final, Protocol, cast

ROOT: Final = Path(__file__).resolve().parent.parent
PIN_PATH: Final = Path("scripts/figure_vocabulary_pin.toml")
MODULE_PATH: Final = Path("src/agents/figure_vocabulary.py")
VOCABULARY_MEMBER: Final = "package/dist/figure-vocabulary.json"
SUPPORTED_VERSION: Final = 1
_URL: Final = re.compile(r"^https://[\x21\x23-\x5b\x5d-\x7e]+$")
_INTEGRITY: Final = re.compile(r"^sha512-[A-Za-z0-9+/]{86}==$")
_INTEGRITY_PREFIX: Final = "sha512-"
_PIN_COMMENT: Final = (
    "# The figure release whose vocabulary agents generates its enums from. Raise it with\n"
    "# `uv run python scripts/generate_figure_vocabulary.py --pin <url> <integrity>`.\n"
)
_TOKEN: Final = re.compile(r"^[a-z][a-z0-9_]*$")
_FETCH_SECONDS: Final = 60

type Fetch = Callable[[str], bytes]


class UrlOpener(Protocol):
    def __call__(self, url: str, *, timeout: float) -> AbstractContextManager[IO[bytes]]: ...


class VocabularyError(Exception):
    """The pin, the tarball or the vocabulary is not what this script reads."""


@dataclass(frozen=True)
class Pin:
    url: str
    integrity: str


@dataclass(frozen=True)
class Vocabulary:
    gestures: tuple[str, ...]
    moods: tuple[str, ...]


def fetch(url: str, *, open_url: UrlOpener = urllib.request.urlopen) -> bytes:
    with open_url(url, timeout=_FETCH_SECONDS) as response:
        return response.read()


def read_pin(path: Path) -> Pin:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise VocabularyError(f"the pin file {path} is missing") from error
    document = tomllib.loads(text)
    url, integrity = document.get("url"), document.get("integrity")
    if not isinstance(url, str) or not isinstance(integrity, str):
        raise VocabularyError(f"the pin file {path} must hold a url and an integrity string")
    return Pin(url, integrity)


def validate_pin(pin: Pin) -> None:
    if not _URL.fullmatch(pin.url):
        raise VocabularyError(f"the release url must be an https address, not {pin.url!r}")
    if not _INTEGRITY.fullmatch(pin.integrity):
        raise VocabularyError("the integrity must be an npm sha512 integrity")


def write_pin(path: Path, pin: Pin) -> None:
    pin_text = f"url = {json.dumps(pin.url)}\nintegrity = {json.dumps(pin.integrity)}\n"
    path.write_text(_PIN_COMMENT + pin_text, encoding="utf-8")


def check_integrity(tarball: bytes, integrity: str) -> None:
    digest = base64.b64encode(hashlib.sha512(tarball).digest()).decode("ascii")
    if f"{_INTEGRITY_PREFIX}{digest}" != integrity:
        raise VocabularyError("the tarball does not match the pinned integrity")


def vocabulary_bytes(tarball: bytes) -> bytes:
    """The vocabulary file, read in memory from the tarball without extracting it."""
    try:
        archive = tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz")
    except tarfile.ReadError as error:
        raise VocabularyError("the tarball is no gzip tar archive") from error
    with archive:
        try:
            member = archive.getmember(VOCABULARY_MEMBER)
        except KeyError as error:
            raise VocabularyError(f"the tarball holds no {VOCABULARY_MEMBER}") from error
        file = archive.extractfile(member)
        if file is None:
            raise VocabularyError(f"{VOCABULARY_MEMBER} in the tarball is no file")
        return file.read()


def _tokens(document: dict[str, object], key: str) -> tuple[str, ...]:
    values = document.get(key)
    if not isinstance(values, list) or not values:
        raise VocabularyError(f"the vocabulary needs a non-empty list {key!r}")
    tokens: list[str] = []
    for value in cast("list[object]", values):
        if not isinstance(value, str) or not _TOKEN.fullmatch(value):
            raise VocabularyError(f"{key!r} holds {value!r}, which is no token like 'wave'")
        if value in tokens:
            raise VocabularyError(f"{key!r} holds {value!r} twice")
        tokens.append(value)
    return tuple(tokens)


def parse_vocabulary(raw: bytes) -> Vocabulary:
    """Reads ``gestures`` and ``moods`` only; every other key is ignored."""
    try:
        document: object = json.loads(raw)
    except ValueError as error:
        raise VocabularyError("the vocabulary is no JSON") from error
    if not isinstance(document, dict):
        raise VocabularyError("the vocabulary must be a JSON object")
    fields = cast("dict[str, object]", document)
    version = fields.get("version")
    if type(version) is not int or version != SUPPORTED_VERSION:
        raise VocabularyError(f"the vocabulary version must be {SUPPORTED_VERSION}")
    return Vocabulary(_tokens(fields, "gestures"), _tokens(fields, "moods"))


def _enum_source(name: str, tokens: tuple[str, ...], doc: str) -> str:
    members = "\n".join(f'    {token.upper()} = "{token}"' for token in tokens)
    return f'class {name}(StrEnum):\n    """{doc}"""\n\n{members}\n'


def render(pin: Pin, vocabulary: Vocabulary) -> str:
    header = (
        '"""The gestures and the moods an agent can show, as figure\'s release names them.\n\n'
        "Generated by scripts/generate_figure_vocabulary.py; do not edit. Figure release:\n"
        f"{pin.url}\n"
        "Integrity:\n"
        f'{pin.integrity}\n"""\n'
    )
    imports = 'from enum import StrEnum\n\n__all__ = ["FigureGesture", "FigureMood"]\n'
    gestures = _enum_source("FigureGesture", vocabulary.gestures, "A gesture figure can play.")
    moods = _enum_source("FigureMood", vocabulary.moods, "A mood figure can show on a face.")
    return f"{header}\n{imports}\n\n{gestures}\n\n{moods}"


def generate(root: Path, pin: Pin, tarball: bytes) -> None:
    check_integrity(tarball, pin.integrity)
    vocabulary = parse_vocabulary(vocabulary_bytes(tarball))
    (root / MODULE_PATH).write_text(render(pin, vocabulary), encoding="utf-8")


def main(argv: Sequence[str], *, root: Path = ROOT, download: Fetch = fetch) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pin",
        nargs=2,
        metavar=("URL", "INTEGRITY"),
        help="raise the pin to this release, once its integrity holds",
    )
    arguments = parser.parse_args(argv)
    try:
        pin = Pin(*arguments.pin) if arguments.pin else read_pin(root / PIN_PATH)
        validate_pin(pin)
        generate(root, pin, download(pin.url))
    except VocabularyError as error:
        print(f"error: {error}")
        return 1
    if arguments.pin:
        write_pin(root / PIN_PATH, pin)
    print(f"wrote {MODULE_PATH} from {pin.url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
