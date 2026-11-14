"""The generator turns a pinned figure release into the enums, or fails loud."""

from __future__ import annotations

import importlib.util
import io
import runpy
import sys
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest

import scripts.generate_figure_vocabulary as generator
from tests.support.figure_release import (
    RELEASE_URL,
    directory_tarball,
    integrity_of,
    pin_for,
    repository,
    tarball,
    vocabulary_document,
)


def run(root: Path, content: bytes, *argv: str) -> int:
    return generator.main(argv, root=root, download=lambda _url: content)


def generated_enums(root: Path) -> tuple[Any, Any]:
    spec = importlib.util.spec_from_file_location("generated", root / generator.MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.FigureGesture, module.FigureMood


def test_the_enums_hold_the_release_tokens_in_order_and_the_header_names_the_release(
    tmp_path: Path,
) -> None:
    release = tarball(vocabulary_document() | {"framings": ["close"]})
    root = repository(tmp_path, pin_for(release))

    assert run(root, release) == 0

    gestures, moods = generated_enums(root)
    document = vocabulary_document()
    assert [gesture.value for gesture in gestures] == document["gestures"]
    assert [mood.value for mood in moods] == document["moods"]
    header = (root / generator.MODULE_PATH).read_text(encoding="utf-8")
    assert RELEASE_URL in header
    assert integrity_of(release) in header


def test_a_second_run_leaves_the_module_as_it_was(tmp_path: Path) -> None:
    release = tarball()
    root = repository(tmp_path, pin_for(release))
    run(root, release)
    first = (root / generator.MODULE_PATH).read_bytes()

    assert run(root, release) == 0

    assert (root / generator.MODULE_PATH).read_bytes() == first


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(tarball(vocabulary_document(version=2)), id="another-version"),
        pytest.param(tarball(vocabulary_document(version=True)), id="boolean-version"),
        pytest.param(tarball(vocabulary_document(version=1.0)), id="float-version"),
        pytest.param(b"not a tarball", id="not-a-tarball"),
        pytest.param(tarball({"gestures": ["wave"], "moods": ["happy"]}), id="no-version"),
        pytest.param(tarball({"version": 1, "moods": ["happy"]}), id="no-gestures"),
        pytest.param(tarball({"version": 1, "gestures": ["wave"]}), id="no-moods"),
        pytest.param(tarball(vocabulary_document(gestures="wave")), id="gestures-not-a-list"),
        pytest.param(tarball(vocabulary_document(moods=[])), id="empty-moods"),
        pytest.param(tarball(vocabulary_document(gestures=["wave", "wave"])), id="repeated"),
        pytest.param(tarball(vocabulary_document(gestures=["Wave"])), id="upper-case"),
        pytest.param(tarball(vocabulary_document(gestures=["1wave"])), id="leading-digit"),
        pytest.param(tarball(vocabulary_document(gestures=["wa-ve"])), id="hyphen"),
        pytest.param(tarball(vocabulary_document(gestures=[""])), id="empty-token"),
        pytest.param(tarball(vocabulary_document(gestures=[7])), id="not-a-string"),
        pytest.param(tarball(b"not json"), id="no-json"),
        pytest.param(tarball(b"[]"), id="not-an-object"),
        pytest.param(tarball(member="package/dist/other.json"), id="file-missing"),
        pytest.param(directory_tarball(), id="member-is-no-file"),
    ],
)
def test_a_release_the_generator_cannot_read_fails_loud_and_writes_nothing(
    tmp_path: Path, content: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path, pin_for(content))

    assert run(root, content) == 1

    assert capsys.readouterr().out.startswith("error: ")
    assert not (root / generator.MODULE_PATH).exists()


@pytest.mark.parametrize(
    "integrity",
    [
        pytest.param(integrity_of(b"another tarball"), id="other-bytes"),
        pytest.param("md5-AAAA", id="not-sha512"),
    ],
)
def test_a_tarball_that_does_not_match_the_pin_is_refused(tmp_path: Path, integrity: str) -> None:
    release = tarball()
    root = repository(tmp_path, generator.Pin(RELEASE_URL, integrity))

    assert run(root, release) == 1

    assert not (root / generator.MODULE_PATH).exists()


@pytest.mark.parametrize(
    "pin_text", [None, 'url = "https://releases.example/a.tgz"\n'], ids=["missing", "no-integrity"]
)
def test_a_missing_or_incomplete_pin_file_fails_loud(tmp_path: Path, pin_text: str | None) -> None:
    root = repository(tmp_path, pin_for(b""))
    pin_file = root / generator.PIN_PATH
    if pin_text is None:
        pin_file.unlink()
    else:
        pin_file.write_text(pin_text, encoding="utf-8")

    assert run(root, tarball()) == 1


def test_raising_the_pin_rewrites_the_pin_file_under_its_comment(tmp_path: Path) -> None:
    older = tarball()
    newer = tarball(vocabulary_document(gestures=["wave", "bow"]))
    root = repository(tmp_path, pin_for(older))

    assert run(root, newer, "--pin", RELEASE_URL, integrity_of(newer)) == 0

    assert generator.read_pin(root / generator.PIN_PATH) == generator.Pin(
        RELEASE_URL, integrity_of(newer)
    )
    assert (root / generator.PIN_PATH).read_text(encoding="utf-8").startswith("#")
    assert "BOW" in (root / generator.MODULE_PATH).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("url", "integrity_suffix"),
    [
        pytest.param("http://releases.example/a.tgz", "", id="not-https"),
        pytest.param("file:///etc/passwd", "", id="not-a-web-address"),
        pytest.param('https://releases.example/a"\nb = 1', "", id="quote-in-url"),
        pytest.param("https://releases.example/a\U0001f600.tgz", "", id="astral-character-in-url"),
        pytest.param(RELEASE_URL, '"\nb = 1', id="text-after-the-integrity"),
    ],
)
def test_raising_the_pin_to_a_malformed_url_or_integrity_is_refused_and_leaves_the_pin(
    tmp_path: Path, url: str, integrity_suffix: str
) -> None:
    release = tarball()
    root = repository(tmp_path, pin_for(release))
    pin_file = root / generator.PIN_PATH
    before = pin_file.read_text(encoding="utf-8")

    assert run(root, release, "--pin", url, integrity_of(release) + integrity_suffix) == 1

    assert pin_file.read_text(encoding="utf-8") == before
    assert not (root / generator.MODULE_PATH).exists()


def test_raising_the_pin_to_a_tarball_that_does_not_match_leaves_the_pin(tmp_path: Path) -> None:
    older = tarball()
    root = repository(tmp_path, pin_for(older))
    pin_file = root / generator.PIN_PATH
    before = pin_file.read_text(encoding="utf-8")

    assert run(root, tarball(), "--pin", RELEASE_URL, integrity_of(b"other")) == 1

    assert pin_file.read_text(encoding="utf-8") == before


def test_run_as_a_program_a_bad_pin_exits_one_with_the_error_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    script = Path(generator.__file__)
    monkeypatch.setattr(sys, "argv", [str(script), "--pin", "http://releases.example/a.tgz", "x"])

    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(script), run_name="__main__")

    assert raised.value.code == 1
    assert capsys.readouterr().out.startswith("error: ")


def test_fetch_returns_the_bytes_the_address_serves() -> None:
    served = tarball()
    asked: list[str] = []

    def open_url(url: str, *, timeout: float) -> AbstractContextManager[io.BytesIO]:
        asked.append(url)
        return io.BytesIO(served)

    assert generator.fetch(RELEASE_URL, open_url=open_url) == served
    assert asked == [RELEASE_URL]
