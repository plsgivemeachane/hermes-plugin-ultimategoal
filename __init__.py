"""Root shim for ``hermes plugins install <owner>/hermes-plugin-ultimategoal``.

``plugins install`` clones the repo and checks its ROOT for ``plugin.yaml`` / ``plugin.json`` /
``__init__.py`` (``hermes_cli/plugins_cmd.py::_looks_like_plugin_dir``). The real plugin lives in
``plugins/ultimategoal/`` because the profile distribution layout needs that path. This shim
makes the bare repo a valid plugin too, so both install routes work:

    hermes plugins install plsgivemeachane/hermes-plugin-ultimategoal --enable
    hermes profile install plsgivemeachane/hermes-plugin-ultimategoal

Do not edit the plugin here — it is the single source of truth under ``plugins/ultimategoal/``.
"""

from .plugins.ultimategoal import register  # noqa: F401

__all__ = ["register"]