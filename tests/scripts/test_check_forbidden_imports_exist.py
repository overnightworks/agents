"""The check refuses a forbidden package that no distribution provides."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

import scripts.check_forbidden_imports_exist as check

CONTRACTS = """
[importlinter:contract:first]
type = forbidden
source_modules =
    agents
forbidden_modules =
    alpha
    beta.sub.**
    agents.inner

[importlinter:contract:second]
type = protected
protected_modules =
    gamma
"""


def test_a_forbidden_module_counts_by_its_top_level_package_without_the_own_one() -> None:
    assert check.forbidden_packages(CONTRACTS) == {"alpha", "beta"}


def test_a_locked_distribution_counts_as_the_import_name_it_is_spelled_as() -> None:
    lock = '[[package]]\nname = "Alpha-Kit"\n\n[[package]]\nname = "beta"\n'
    assert check.locked_modules(lock) == {"alpha_kit", "beta"}


def test_a_forbidden_package_that_nothing_provides_is_unprovided() -> None:
    assert check.unprovided_packages({"alpha", "typo"}, {"alpha"}) == {"typo"}


def test_a_package_kept_out_of_the_tree_needs_no_distribution() -> None:
    kept_out = next(iter(check.UNINSTALLED_PACKAGES))
    assert check.unprovided_packages({kept_out}, set()) == frozenset()


@pytest.mark.parametrize(("forbidden", "exit_code"), [("alpha", 0), ("typo", 1)])
def test_the_check_exits_nonzero_only_for_an_unprovided_name(
    tmp_path: Path, forbidden: str, exit_code: int
) -> None:
    (tmp_path / ".importlinter").write_text(
        f"[importlinter:contract:c]\ntype = forbidden\nforbidden_modules =\n    {forbidden}\n",
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text('[[package]]\nname = "alpha"\n', encoding="utf-8")
    assert check.main(root=tmp_path) == exit_code


def test_run_as_a_script_on_this_repository_every_forbidden_import_names_a_real_package() -> None:
    script = str(Path(check.__file__).resolve())
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(script, run_name="__main__")

    assert raised.value.code == 0
