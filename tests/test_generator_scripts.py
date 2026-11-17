"""Run as a script, each generator writes its file under the repository root."""

from __future__ import annotations

import runpy
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

import scripts.generate_agents_vocabulary as agents_vocabulary
import scripts.generate_member_tool_wire as member_tool_wire


@pytest.mark.parametrize(
    ("generator", "script_name", "written_path"),
    [
        (agents_vocabulary, "generate_agents_vocabulary.py", agents_vocabulary.VOCABULARY_PATH),
        (member_tool_wire, "generate_member_tool_wire.py", member_tool_wire.WIRE_PATH),
    ],
)
def test_run_as_a_script_it_writes_the_file_under_the_repository_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    generator: ModuleType,
    script_name: str,
    written_path: Path,
) -> None:
    render: Callable[[], str] = generator.render
    script = generator.ROOT / "scripts" / script_name
    write_text = Path.write_text

    def write_under_the_test_root(self: Path, data: str, *, encoding: str) -> int:
        return write_text(tmp_path / self.relative_to(generator.ROOT), data, encoding=encoding)

    (tmp_path / written_path.parent).mkdir(parents=True)
    monkeypatch.setattr(Path, "write_text", write_under_the_test_root)

    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(script), run_name="__main__")

    monkeypatch.undo()
    assert raised.value.code == 0
    assert (tmp_path / written_path).read_text(encoding="utf-8") == render()
