import os
import warnings

import yaml

from .definitions import (
    CONFIG_PATH,
    CONFIG_EXAMPLE_PATH,
    DEFAULT_SETTINGS,
    OPTIONAL_KEYS,
    THEMES,
    flatten_dict,
)

# Fall back to the example config when config.yaml is absent (fresh checkout,
# CI, tests). The bot still needs a real token to actually connect.
if os.path.exists(CONFIG_PATH):
    _config_path = CONFIG_PATH
else:
    warnings.warn("config.yaml not found — falling back to config_example.yaml. Create config.yaml before running for real.")
    _config_path = CONFIG_EXAMPLE_PATH

config = yaml.safe_load(open(_config_path, encoding="utf8"))
config_example = yaml.safe_load(open(CONFIG_EXAMPLE_PATH, encoding="utf8"))


for setting, default_value in DEFAULT_SETTINGS.items():
    if setting not in config:
        config[setting] = default_value


def checkConfig():
    """Keys in the example that the user's config is missing, excluding the
    optional ones. Add new optional keys to OPTIONAL_KEYS in definitions.py,
    not just to config_example.yaml. `make config-check` reports the same
    drift ahead of a restart; `make config-migrate` closes it."""
    present = flatten_dict(config)
    return [key for key in flatten_dict(config_example)
            if key not in present and key not in OPTIONAL_KEYS]


def checkConfigValues():
    wrongValues = []
    languages = ["de-de", "en-us", "es-es", "fr-fr", "it-it", "nl-be", "pl-pl", "pt-pt", "ru-ru"]
    if config["language"] not in languages:
        wrongValues.append("language")
    if str(config.get("theme", "default")).strip().lower() not in THEMES:
        wrongValues.append("theme")
    return wrongValues
