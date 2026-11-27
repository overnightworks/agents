"""Arrangement for running a generator script as a program under a temporary repository root."""

from __future__ import annotations

import runpy
from pathlib import Path
from types import ModuleType

import pytest


def assert_script_writes_its_file_under_the_root(
    generator: ModuleType,
    script_name: str,
    written_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    assert (tmp_path / written_path).read_text(encoding="utf-8") == generator.render()
