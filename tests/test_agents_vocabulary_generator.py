"""The generator writes the closed set of stop outcomes that the wheel carries."""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

import scripts.generate_agents_vocabulary as generator
from agents.turn import StopOutcome


def written(root: Path) -> dict[str, object]:
    return json.loads((root / generator.VOCABULARY_PATH).read_text(encoding="utf-8"))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / generator.VOCABULARY_PATH.parent).mkdir(parents=True)
    return tmp_path


def test_the_file_holds_the_stop_outcomes_in_declared_order_at_shape_version_one(
    root: Path,
) -> None:
    assert generator.main(root=root) == 0

    document = written(root)
    assert [StopOutcome(value) for value in document["stop_outcomes"]] == list(StopOutcome)
    assert document["version"] == 1


def test_a_second_run_leaves_the_file_as_it_was(root: Path) -> None:
    generator.main(root=root)
    first = (root / generator.VOCABULARY_PATH).read_bytes()

    generator.main(root=root)

    assert (root / generator.VOCABULARY_PATH).read_bytes() == first


def test_run_as_a_script_it_writes_the_file_under_the_repository_root(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = generator.ROOT / "scripts" / "generate_agents_vocabulary.py"
    write_text = Path.write_text

    def write_under_the_test_root(self: Path, data: str, *, encoding: str) -> int:
        return write_text(root / self.relative_to(generator.ROOT), data, encoding=encoding)

    monkeypatch.setattr(Path, "write_text", write_under_the_test_root)

    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(script), run_name="__main__")

    monkeypatch.undo()
    assert raised.value.code == 0
    assert (root / generator.VOCABULARY_PATH).read_text(encoding="utf-8") == generator.render()
