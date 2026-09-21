"""Symbolic workspace resolution for the Agent Execution Bridge (V0.2, Phase E.3A).

``AgentExecution.workspace_ref`` is a **symbolic key**, never a caller-supplied
filesystem path. Its shape is already constrained by the database
(``ck_agent_execution_workspace_ref_symbolic``), but shape is not authority: the
only thing that turns a key into a directory is the static mapping configured on
the DX side. A caller can therefore never name a path, only a key DX already knows.

Resolution pipeline (frozen by the E.3A guard order):

* key -> static configured mapping lookup
* canonicalize the configured path (``Path.resolve`` / realpath)
* the resolved path must exist and be a directory
* the resolved path must remain inside the permitted root of that key
* a path whose final component is a symlink/junction pointing elsewhere is rejected
* the canonical absolute ``Path`` is returned

The permitted root is the configured path with *its parent* canonically resolved.
That distinction matters on both platforms: a symlinked ancestor of a legitimate
workspace directory is tolerated, while the escape case -- the configured
directory itself being a symlink or junction that redirects outside -- is not.

Fail closed on every failure mode: unknown key, a ``None`` key, a non-absolute
configured path, a missing path, a file instead of a directory, an unreadable or
otherwise unusable path, and a resolved path outside the permitted root. There is
deliberately **no fallback** to the current working directory, the repository
root, ``/``, or the user's HOME, and there is no error-code vocabulary beyond
``workspace_denied``.

This module performs reads only: no network, no subprocess, no writes, no
database access, and no global state.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


class WorkspaceDenied(Exception):
    """A symbolic ``workspace_ref`` does not resolve to a permitted directory.

    Every failure mode collapses into this single classification; the message
    carries the discriminating detail for the audit trail and is never used as a
    control-flow signal by callers.
    """


class WorkspaceResolver:
    """Resolve symbolic workspace keys against a static, immutable mapping."""

    def __init__(self, mapping: Mapping[str, str]) -> None:
        if not isinstance(mapping, Mapping):
            raise TypeError("workspace mapping must be a mapping of key to path")
        # Copied so a later mutation of the caller's dict cannot widen authority
        # between the guard check and the provider's pre-launch re-check.
        self._mapping: dict[str, str] = {str(key): value for key, value in mapping.items()}

    @property
    def keys(self) -> tuple[str, ...]:
        """The configured symbolic keys, for diagnostics only."""

        return tuple(sorted(self._mapping))

    def resolve(self, workspace_ref: str | None) -> Path:
        """Return the canonical absolute directory for ``workspace_ref``.

        Raises ``WorkspaceDenied`` for every failure mode; never returns a path the
        caller did not configure.
        """

        if not isinstance(workspace_ref, str) or not workspace_ref:
            raise WorkspaceDenied("workspace_ref is not a configured symbolic key")

        configured_value = self._mapping.get(workspace_ref)
        if not isinstance(configured_value, str) or not configured_value:
            raise WorkspaceDenied(f"workspace_ref {workspace_ref!r} is not configured")

        configured = Path(configured_value)
        if not configured.is_absolute():
            # A relative configured path would silently anchor to the process working
            # directory, which is exactly the fallback this resolver must not have.
            raise WorkspaceDenied(
                f"configured workspace path for {workspace_ref!r} must be absolute"
            )

        try:
            permitted_root = configured.parent.resolve() / configured.name
            resolved = configured.resolve()
            is_directory = resolved.is_dir()
            is_readable = os.access(resolved, os.R_OK)
        except (OSError, ValueError) as error:
            raise WorkspaceDenied(
                f"configured workspace path for {workspace_ref!r} is not usable: {error}"
            ) from error

        if not is_directory:
            raise WorkspaceDenied(
                f"configured workspace path for {workspace_ref!r} is not a directory"
            )
        if not is_readable:
            raise WorkspaceDenied(
                f"configured workspace path for {workspace_ref!r} is not readable"
            )
        if resolved != permitted_root and permitted_root not in resolved.parents:
            raise WorkspaceDenied(
                f"workspace {workspace_ref!r} resolves outside its permitted root"
            )
        return resolved
