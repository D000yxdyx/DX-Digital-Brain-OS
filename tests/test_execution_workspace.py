"""Phase E.3A tests: the symbolic workspace resolver.

``workspace_ref`` is a symbolic key, never a path. These tests prove the resolver is
closed over the static mapping DX configured and over the filesystem reality behind it,
and that every failure mode collapses into a single ``workspace_denied``
classification.

Scope discipline: this module touches no database, no session, no gateway, and no
migration. It is a pure filesystem test, so it runs without the integration database
and deliberately does not request the ``session_factories`` fixture.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from dxbrain.execution.workspace import WorkspaceDenied, WorkspaceResolver


def _directory_reparse_point(link: Path, target: Path) -> None:
    """Create a directory symlink, falling back to a Windows directory junction.

    A junction is also a redirecting reparse point, so it exercises exactly the same
    escape path as a symlink. The fallback exists because creating a symlink on Windows
    needs Developer Mode or the symlink privilege, while ``mklink /J`` does not.

    If neither can be created this is an environment that cannot express the scenario in
    any form; the test fails rather than skipping, so an unproven guard is never
    reported as green.
    """

    try:
        os.symlink(target, link, target_is_directory=True)
        if os.path.lexists(link):
            return
    except OSError:
        pass

    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
        if completed.returncode == 0 and os.path.lexists(link):
            return

    pytest.fail(
        "could not create a directory symlink or junction; the symlink-escape guard "
        "cannot be proven on this machine"
    )


def test_unknown_workspace_ref_is_denied(tmp_path: Path) -> None:
    resolver = WorkspaceResolver({"default": str(tmp_path)})

    with pytest.raises(WorkspaceDenied):
        resolver.resolve("not-configured")


def test_missing_workspace_ref_is_denied(tmp_path: Path) -> None:
    """A ``None`` key is not a configured key, so it fails closed too."""

    resolver = WorkspaceResolver({"default": str(tmp_path)})

    with pytest.raises(WorkspaceDenied):
        resolver.resolve(None)


def test_configured_path_missing_is_denied(tmp_path: Path) -> None:
    resolver = WorkspaceResolver({"default": str(tmp_path / "absent")})

    with pytest.raises(WorkspaceDenied):
        resolver.resolve("default")


def test_configured_path_that_is_a_file_is_denied(tmp_path: Path) -> None:
    target = tmp_path / "not-a-directory.txt"
    target.write_text("content", encoding="utf-8")
    resolver = WorkspaceResolver({"default": str(target)})

    with pytest.raises(WorkspaceDenied):
        resolver.resolve("default")


def test_relative_configured_path_is_denied() -> None:
    """No cwd fallback: a relative configured path cannot silently anchor to the process."""

    resolver = WorkspaceResolver({"default": "relative/workspace"})

    with pytest.raises(WorkspaceDenied):
        resolver.resolve("default")


def test_symlink_escape_is_denied(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x", encoding="utf-8")
    link = tmp_path / "escape"
    _directory_reparse_point(link, outside)

    resolver = WorkspaceResolver({"default": str(link)})

    # The redirecting link exists and is a directory, so only the escape check can
    # reject it -- proving the check, not a path-existence accident.
    assert Path(link).is_dir()

    with pytest.raises(WorkspaceDenied):
        resolver.resolve("default")


def test_valid_workspace_returns_canonical_absolute_path(tmp_path: Path) -> None:
    workspace = tmp_path / "default"
    workspace.mkdir()
    resolver = WorkspaceResolver({"default": str(workspace)})

    resolved = resolver.resolve("default")

    assert isinstance(resolved, Path)
    assert resolved.is_absolute()
    assert resolved == workspace.resolve()
    assert resolved.is_dir()


def test_dot_segments_are_canonicalized_inside_the_permitted_root(tmp_path: Path) -> None:
    """``..`` inside the configured value must not restart the escape check from cwd."""

    workspace = tmp_path / "default"
    workspace.mkdir()
    detoured = tmp_path / "sibling" / ".." / "default"
    resolver = WorkspaceResolver({"default": str(detoured)})

    assert resolver.resolve("default") == workspace.resolve()


def test_mapping_keys_are_reported_for_diagnostics(tmp_path: Path) -> None:
    resolver = WorkspaceResolver({"b": str(tmp_path), "a": str(tmp_path)})

    assert resolver.keys == ("a", "b")


def test_resolver_copies_the_mapping(tmp_path: Path) -> None:
    """Authority cannot be widened after construction by mutating the caller's dict."""

    source = {"default": str(tmp_path)}
    resolver = WorkspaceResolver(source)
    source["default"] = str(tmp_path / "somewhere-else")

    assert resolver.resolve("default") == tmp_path.resolve()
