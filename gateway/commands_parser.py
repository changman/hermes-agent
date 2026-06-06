"""Dynamic commands builder for Skytower skills response.

Builds a categorized commands dict from COMMAND_REGISTRY so that the
Skytower Web UI can display all available commands without hardcoding.

Cache is process-scoped (invalidated by invalidate_commands_cache()).
Call invalidate_commands_cache() after /reload-skills or /reload-mcp
so the next request picks up newly installed skills.
"""

from __future__ import annotations

import logging
from typing import Dict, List

logger = logging.getLogger(__name__)

_commands_cache: Dict[str, List[Dict[str, str]]] | None = None


def get_hermes_commands() -> Dict[str, List[Dict[str, str]]]:
    """Return all gateway-available built-in commands grouped by category.

    Returns:
        {category: [{command: "/name", description: "..."}, ...], ...}
    """
    global _commands_cache
    if _commands_cache is not None:
        return _commands_cache

    _commands_cache = _build_commands()
    total = sum(len(v) for v in _commands_cache.values())
    logger.debug(
        "[commands] built %d categories, %d commands",
        len(_commands_cache), total,
    )
    return _commands_cache


def invalidate_commands_cache() -> None:
    """Discard the cached commands so the next call re-builds them."""
    global _commands_cache
    _commands_cache = None
    logger.debug("[commands] cache invalidated")


def _build_commands() -> Dict[str, List[Dict[str, str]]]:
    """Build the commands dict from COMMAND_REGISTRY."""
    try:
        from hermes_cli.commands import COMMAND_REGISTRY, _is_gateway_available, _resolve_config_gates
    except Exception as exc:
        logger.warning("[commands] failed to import COMMAND_REGISTRY: %s", exc)
        return {}

    try:
        overrides = _resolve_config_gates()
    except Exception:
        overrides = set()

    result: Dict[str, List[Dict[str, str]]] = {}
    for cmd in COMMAND_REGISTRY:
        if not _is_gateway_available(cmd, overrides):
            continue
        cat = cmd.category
        if cat not in result:
            result[cat] = []
        desc = cmd.description
        if cmd.args_hint:
            desc = f"{desc} (usage: /{cmd.name} {cmd.args_hint})"
        result[cat].append({"command": f"/{cmd.name}", "description": desc})

    return result
