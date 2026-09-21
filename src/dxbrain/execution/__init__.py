"""Agent / workforce execution namespace (ADR-0010, Agent Execution Bridge V0.2).

This package is deliberately parallel to ``dxbrain.providers`` rather than inside it:

* ``dxbrain.providers`` -- model-call providers (ADR-0005): one model request/response.
* ``dxbrain.execution`` -- agent/workforce execution (ADR-0010): one governed execution
  unit that may internally contain many model calls, tool calls and file operations.

The two abstractions have different owners, different lifecycles and different ledgers.
They must never share a registry, a lifecycle, or a naming scheme.
"""
