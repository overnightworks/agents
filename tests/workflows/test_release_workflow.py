"""The release build refuses all but the client wheel; its release note names the git tag."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"
_WIRE_SCRIPT = Path("scripts/member_tool_wire_diff.py")
_WIRE_SNAPSHOT = Path("scripts/member-tool-wire.json")
NO_SNAPSHOT = "changed (the previous release has no snapshot to compare)."
_GIT_ENV = {
    "PATH": "/usr/bin:/bin",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}
_WITHIN_STEP = r"(?:(?!      - ).*\n)*?"


@dataclass(frozen=True)
class _Release:
    """The scenario every test of one release arranges: what is dispatched and what it builds."""

    version: str = "0.3.0"
    repository: str = "overnightworks/agents"
    wheel_contents: tuple[tuple[str, bytes], ...] = (
        ("overnightworks_agents-{}-py3-none-any.whl", b"client"),
    )
    commit: str = ""


@pytest.fixture
def release() -> _Release:
    return _Release()


def _run_script(step_name: str) -> str:
    step = re.search(
        rf"- name: {re.escape(step_name)}\n{_WITHIN_STEP}        run: \|\n((?:          .*\n|\n)+)",
        _WORKFLOW.read_text(encoding="utf-8"),
    )
    assert step, f"release.yml has no run script for {step_name}"
    return "\n".join(line[10:] for line in step.group(1).splitlines())


def _run_step(
    step_name: str, tmp_path: Path, release: _Release
) -> subprocess.CompletedProcess[str]:
    """Run one step's script in `tmp_path`, with `tmp_path/bin` first on the path."""
    output = tmp_path / "github_output"
    output.touch()
    (tmp_path / "runner_temp").mkdir(exist_ok=True)
    return subprocess.run(
        ["/bin/bash", "-e", "-c", _run_script(step_name)],
        cwd=tmp_path,
        env={
            "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
            "GITHUB_OUTPUT": str(output),
            "RUNNER_TEMP": str(tmp_path / "runner_temp"),
            "GITHUB_REPOSITORY": release.repository,
            "VERSION": release.version,
            "COMMIT": release.commit,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def _checkout_at_head(tmp_path: Path, release: _Release) -> _Release:
    """Make `tmp_path` a git checkout and return the scenario that gated its head commit."""
    for command in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "head"]):
        subprocess.run(["git", *command], cwd=tmp_path, env=_GIT_ENV, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        env=_GIT_ENV,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return replace(release, commit=head)


def _wheels_in(tmp_path: Path, release: _Release, version: str | None = None) -> dict[str, bytes]:
    wheels = {
        name.format(version or release.version): content for name, content in release.wheel_contents
    }
    (tmp_path / "dist").mkdir(exist_ok=True)
    for name, content in wheels.items():
        (tmp_path / "dist" / name).write_bytes(content)
    return wheels


@pytest.mark.parametrize(
    ("built_version", "passes"),
    [(None, True), ("0.2.0", False)],
    ids=["at the version", "at another version"],
)
def test_the_build_must_hold_exactly_the_client_wheel_at_the_version(
    tmp_path, release, built_version, passes
):
    _wheels_in(tmp_path, release, built_version)

    result = _run_step(
        "Refuse unless the build holds exactly the client wheel at the version",
        tmp_path,
        release,
    )

    assert (result.returncode == 0) is passes
    assert passes or result.stderr.startswith("Refused:")


@pytest.mark.parametrize("gated_is_head", [True, False], ids=["the gated commit", "another commit"])
def test_the_build_must_check_out_the_commit_the_gate_judged(tmp_path, release, gated_is_head):
    checked_out = _checkout_at_head(tmp_path, release)
    gated = checked_out if gated_is_head else replace(checked_out, commit="0" * 40)

    result = _run_step("Refuse unless the checkout is the commit the gate judged", tmp_path, gated)

    assert (result.returncode == 0) is gated_is_head
    assert gated_is_head or (
        result.stderr
        == f"Refused: the checkout is {checked_out.commit}, not the gated commit {gated.commit}.\n"
    )


def test_a_missing_or_extra_wheel_is_refused(tmp_path, release):
    wheels = _wheels_in(tmp_path, release)
    (tmp_path / "dist" / next(iter(wheels))).unlink()
    step = "Refuse unless the build holds exactly the client wheel at the version"

    missing = _run_step(step, tmp_path, release)
    _wheels_in(tmp_path, release)
    (tmp_path / "dist" / f"extra-{release.version}-py3-none-any.whl").touch()
    extra = _run_step(step, tmp_path, release)

    assert (missing.returncode, extra.returncode) == (1, 1)


def _snapshot(arguments: str) -> str:
    return json.dumps({"version": 1, "tools": {"say": {"arguments": arguments}}})


def _checkout_with_wire(
    tmp_path: Path, current_snapshot: str, previous_tags: dict[str, str | None]
) -> None:
    """Make `tmp_path` a checkout holding the wire script, the current snapshot and tagged releases.

    `previous_tags` maps a release tag to the snapshot it committed, or None for a release without.
    """
    (tmp_path / "scripts").mkdir()
    shutil.copy(_REPOSITORY_ROOT / _WIRE_SCRIPT, tmp_path / _WIRE_SCRIPT)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, env=_GIT_ENV, check=True)
    for tag, snapshot in previous_tags.items():
        (tmp_path / _WIRE_SNAPSHOT).unlink(missing_ok=True)
        if snapshot is not None:
            (tmp_path / _WIRE_SNAPSHOT).write_text(snapshot, encoding="utf-8")
        for command in (["add", "-A"], ["commit", "-q", "--allow-empty", "-m", tag], ["tag", tag]):
            subprocess.run(["git", *command], cwd=tmp_path, env=_GIT_ENV, check=True)
    (tmp_path / _WIRE_SNAPSHOT).write_text(current_snapshot, encoding="utf-8")


def _release_note(
    tmp_path: Path,
    release: _Release,
    previous_tags: dict[str, str | None] | None = None,
    current_snapshot: str = "{}",
) -> str:
    _checkout_with_wire(tmp_path, current_snapshot, previous_tags or {})
    result = _run_step("Write the release note naming the git tag", tmp_path, release)
    assert result.returncode == 0, result.stderr
    return (tmp_path / "github_output").read_text(encoding="utf-8")


def test_the_release_page_names_the_git_tag_requirement_and_no_wheel_url(tmp_path, release):
    note = _release_note(tmp_path, release)

    requirement = (
        f"overnightworks-agents @ git+https://github.com/{release.repository}@v{release.version}\n"
    )
    assert requirement in note
    assert "releases/download" not in note


def test_the_release_note_is_written_in_the_multiline_output_form(tmp_path, release):
    lines = _release_note(tmp_path, release).splitlines()

    _, delimiter = lines[0].split("<<")
    assert lines[-1] == delimiter


@pytest.mark.parametrize(
    ("version", "previous_tags", "expected"),
    [
        ("0.3.0", {"v0.2.0": _snapshot("a")}, "unchanged."),
        ("0.3.0", {"v0.2.0": _snapshot("b")}, "changed: say."),
        ("0.3.0", {"v0.2.0": None}, NO_SNAPSHOT),
        ("0.3.0", {}, NO_SNAPSHOT),
        ("0.10.0", {"v0.2.0": _snapshot("b"), "v0.9.0": _snapshot("a")}, "unchanged."),
        ("0.3.0", {"v0.3.0": _snapshot("a"), "v0.4.0": _snapshot("a")}, NO_SNAPSHOT),
    ],
    ids=[
        "previous snapshot equal",
        "previous snapshot differs",
        "previous tag without snapshot",
        "no previous tag",
        "the nearest earlier tag in version order",
        "only the dispatched version and a later tag",
    ],
)
def test_the_release_note_ends_with_the_member_tool_wire_line_against_the_previous_tag(
    tmp_path, release, version, previous_tags, expected
):
    note = _release_note(
        tmp_path, replace(release, version=version), previous_tags, _snapshot("a")
    ).splitlines()

    assert note[-2] == f"**Member-tool wire:** {expected}"
    assert note[-1] == note[0].split("<<")[1]


def test_a_release_run_value_never_interpolates_an_expression():
    run_values = re.findall(
        r"^ +run: (.*(?:\n(?: {10}.*|)$)*)", _WORKFLOW.read_text(encoding="utf-8"), re.MULTILINE
    )

    assert run_values
    assert not [value for value in run_values if "${{" in value]
