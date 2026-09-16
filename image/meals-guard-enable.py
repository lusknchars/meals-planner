#!/usr/bin/env python3
"""Put meals-guard on the agent's enabled list, every boot.

A bundled plugin of kind `standalone` is discovered and then skipped unless its
key is in `plugins.enabled` (hermes_cli/plugins_discovery.py: gate_manifest).
Only `backend` and `platform` kinds auto-load, and the guard is neither.

So the image has to say so itself. Not in the seed config: cont-init copies that
only when config.yaml is ABSENT, which means a home that already exists -- every
installation that upgrades -- would never see the line, and the guard would sit
in the image doing nothing. Written here instead, on every boot, idempotently.

It edits the one key it owns and leaves the rest of the file alone, the way
plow-init's configure() does, and writes through a sibling and a rename so a
boot interrupted mid-write cannot leave a half-parsed config.yaml behind.
"""
import os
import sys

import yaml

CONFIG = "/var/lib/hermes/config.yaml"
PLUGIN_KEY = "meals-guard"


def main() -> int:
    try:
        with open(CONFIG) as handle:
            config = yaml.safe_load(handle) or {}
    except FileNotFoundError:
        # cont-init seeds it before this runs; if it is missing something else
        # is wrong, and writing a config.yaml of our own would hide that.
        print(f"meals-guard: no {CONFIG} yet, leaving it to the seed", file=sys.stderr)
        return 0
    except (OSError, yaml.YAMLError) as error:
        print(f"meals-guard: cannot read {CONFIG} ({error}); not touching it", file=sys.stderr)
        return 0

    plugins = config.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        print("meals-guard: plugins: is not a mapping; leaving it alone", file=sys.stderr)
        return 0
    enabled = plugins.get("enabled")
    enabled = list(enabled) if isinstance(enabled, list) else []
    disabled = plugins.get("disabled")
    disabled = [name for name in disabled if name != PLUGIN_KEY] \
        if isinstance(disabled, list) else None

    already = PLUGIN_KEY in enabled and (disabled is None or PLUGIN_KEY not in disabled)
    if already and (disabled is None or disabled == plugins.get("disabled")):
        return 0

    if PLUGIN_KEY not in enabled:
        enabled.append(PLUGIN_KEY)
    plugins["enabled"] = enabled
    if disabled is not None:
        plugins["disabled"] = disabled

    temporary = f"{CONFIG}.meals-guard"
    try:
        with open(temporary, "w") as handle:
            yaml.safe_dump(config, handle, sort_keys=False)
        os.chmod(temporary, 0o640)
        os.replace(temporary, CONFIG)
    except OSError as error:
        print(f"meals-guard: could not write {CONFIG} ({error})", file=sys.stderr)
        return 0
    print(f"meals-guard: enabled in {CONFIG}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
