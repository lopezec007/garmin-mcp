"""Shared building blocks for tool modules.

Every tool module exposes ``register(mcp, garmin)`` and defines its tools
inside it with ``@mcp.tool(**READ)``. Conventions:

* Tools are ``async`` and reach Garmin only through ``await garmin.call(
  "<garminconnect method name>", *args)`` -- never by touching the client
  directly. ``garmin.call`` handles login, retries and error mapping.
* Date parameters use the ``DateArg`` / ``StartDateArg`` / ``EndDateArg``
  aliases and are resolved with ``await garmin.date(value)`` or
  ``await garmin.date_range(start, end, default_days=N)``.
* Tools are annotated ``-> str`` and end with ``return dump(payload)``, which
  strips Garmin's null noise and emits one compact JSON text block. (Returning
  raw lists/dicts makes the SDK split lists into one content block per item
  and pretty-print everything, which wastes the model's context.)
* The docstring is the tool description the model sees: say what the tool
  returns and when to use it, in one or two sentences.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)

# Keyword arguments for ``@mcp.tool(**READ)`` on every read-only tool.
READ: dict[str, Any] = {"annotations": READ_ONLY, "structured_output": False}

_DATE_HELP = "YYYY-MM-DD, 'today', 'yesterday', or '<N>d' for N days ago (e.g. '7d')"

DateArg = Annotated[
    str | None,
    Field(description=f"Date: {_DATE_HELP}. Defaults to today."),
]
StartDateArg = Annotated[
    str | None,
    Field(description=f"Start date (inclusive): {_DATE_HELP}."),
]
EndDateArg = Annotated[
    str | None,
    Field(description=f"End date (inclusive): {_DATE_HELP}. Defaults to today."),
]

_EMPTY: tuple[Any, ...] = (None, [], {}, "")


def clean(value: Any) -> Any:
    """Recursively drop ``None`` values and empty containers from Garmin payloads."""
    if isinstance(value, Mapping):
        out = {k: clean(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in _EMPTY}
    if isinstance(value, list | tuple):
        return [clean(v) for v in value]
    return value


def dump(value: Any) -> str:
    """Serialise a tool result: ``clean()`` it, then compact JSON."""
    return json.dumps(clean(value), separators=(",", ":"), ensure_ascii=False, default=str)


def pick(mapping: Mapping[str, Any] | None, keys: Iterable[str]) -> dict[str, Any]:
    """Return only ``keys`` from ``mapping`` (missing and ``None`` values skipped)."""
    if not mapping:
        return {}
    return {k: mapping[k] for k in keys if mapping.get(k) is not None}


def omit(mapping: Mapping[str, Any] | None, keys: Iterable[str]) -> dict[str, Any]:
    """Return ``mapping`` without ``keys`` (handy for dropping bulky time series)."""
    if not mapping:
        return {}
    drop = set(keys)
    return {k: v for k, v in mapping.items() if k not in drop}
