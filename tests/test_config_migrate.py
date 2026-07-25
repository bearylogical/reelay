"""Migrating an old config.yaml forward to the current example.

The merge rewrites a file the operator owns and cannot easily reconstruct, so
the bar is: never lose a value, never lose a comment, never write YAML that
doesn't parse -- and refuse rather than guess.
"""

import textwrap

import pytest
import yaml

import reelay.config as cfg
from reelay import config_migrate as mig


EXAMPLE = textwrap.dedent("""\
    # Sonarr Configuration
    sonarr:
      server:
        addr:
        port : 8989 # Default is 8989
      search: true
      # Optional: feed Sonarr imports into the digest.
      webhookSecret:

    telegram:
      token:

    # How many days to wait before nudging.
    reminderDefaultDays: 3

    weeklyDigest:
      enable: false

    language: en-us
    debugLogging: false
    """)

LEGACY = textwrap.dedent("""\
    # Sonarr Configuration
    sonarr:
      server:
        addr: 10.0.0.5
        port : 8989
      search: false

    telegram:
      token: 12345:ABC

    language: en-us
    """)


def _merged(example=EXAMPLE, config=LEGACY):
    text, added = mig.merge(example, config)
    return text, yaml.safe_load(text), added


def test_drift_lists_what_the_example_gained():
    result = mig.drift(EXAMPLE, LEGACY)
    assert result.missing == ["sonarr/webhookSecret", "reminderDefaultDays",
                              "weeklyDigest", "debugLogging"]


def test_drift_separates_optional_keys_from_startup_blocking_ones():
    result = mig.drift(EXAMPLE, LEGACY)
    assert result.optional == ["sonarr/webhookSecret"]  # in OPTIONAL_KEYS
    assert "sonarr/webhookSecret" not in result.required


def test_a_whole_missing_block_is_reported_once_not_per_leaf():
    # weeklyDigest/enable is missing too, but the block is what gets inserted.
    assert "weeklyDigest/enable" not in mig.drift(EXAMPLE, LEGACY).missing


def test_keys_the_example_does_not_have_are_reported_and_left_alone():
    config = LEGACY + "myCustomKey: 42\n"
    assert mig.drift(EXAMPLE, config).unknown == ["myCustomKey"]
    text, parsed, _ = _merged(config=config)
    assert parsed["myCustomKey"] == 42


def test_merge_closes_the_drift_it_reported():
    text, _, added = _merged()
    assert added == mig.drift(EXAMPLE, LEGACY).missing
    assert mig.drift(EXAMPLE, text).missing == []


def test_merge_keeps_every_value_the_operator_had_set():
    _, parsed, _ = _merged()
    assert parsed["sonarr"]["server"]["addr"] == "10.0.0.5"
    assert parsed["telegram"]["token"] == "12345:ABC"
    assert parsed["sonarr"]["search"] is False  # not clobbered by the example's true


def test_merge_brings_the_documentation_with_the_key():
    # A key arriving with no explanation is a key the operator has to go read
    # the example for anyway.
    text, _, _ = _merged()
    assert "# Optional: feed Sonarr imports into the digest." in text
    assert "# How many days to wait before nudging." in text


def test_nested_keys_land_inside_their_parent_block():
    _, parsed, _ = _merged()
    assert parsed["sonarr"]["webhookSecret"] is None
    assert "webhookSecret" not in parsed  # not stranded at the top level


def test_children_land_under_a_parent_that_exists_but_is_empty():
    config = "telegram:\n  token: 12345:ABC\n\nsonarr:\nlanguage: en-us\n"
    _, parsed, _ = _merged(config=config)
    assert parsed["sonarr"]["server"]["port"] == 8989
    assert parsed["sonarr"]["search"] is True


def test_children_land_under_a_parent_that_ends_the_file():
    config = "language: en-us\ntelegram:\n  token: 12345:ABC\n"
    _, parsed, _ = _merged(config=config)
    assert parsed["telegram"]["token"] == "12345:ABC"
    assert parsed["sonarr"]["server"]["port"] == 8989


def test_merge_reindents_a_config_that_nests_differently():
    config = textwrap.dedent("""\
        sonarr:
            server:
                addr: 10.0.0.5
            search: true

        telegram:
          token: 12345:ABC
        language: en-us
        """)
    _, parsed, _ = _merged(config=config)
    assert parsed["sonarr"]["server"]["addr"] == "10.0.0.5"
    assert parsed["sonarr"]["server"]["port"] == 8989  # inserted at the file's own indent


def test_merge_is_idempotent():
    once, _, _ = _merged()
    twice, added = mig.merge(EXAMPLE, once)
    assert added == []
    assert twice.rstrip("\n") == once.rstrip("\n")


def test_merge_preserves_windows_line_endings():
    text, _ = mig.merge(EXAMPLE, LEGACY.replace("\n", "\r\n"))
    assert "\r\n" in text
    assert "\n" not in text.replace("\r\n", "")


def test_merge_refuses_rather_than_writing_a_broken_document():
    # A scalar where the example has a block: splicing children under it would
    # produce nonsense, so it must raise instead of half-writing.
    with pytest.raises(ValueError):
        mig.merge(EXAMPLE, "sonarr: nope\ntelegram:\n  token: x\nlanguage: en-us\n")


def test_the_shipped_example_needs_no_migration_against_itself():
    text = mig._read(mig.CONFIG_EXAMPLE_PATH)
    result = mig.drift(text, text)
    assert result.missing == [] and result.unknown == []


def test_check_agrees_with_what_startup_would_refuse_on(monkeypatch):
    # The point of `make config-check` is to learn this before a restart does.
    text = mig._read(mig.CONFIG_EXAMPLE_PATH)
    stale = "\n".join(line for line in text.splitlines()
                      if not line.startswith("reminderCheckHour")) + "\n"
    assert mig.drift(text, stale).required == ["reminderCheckHour"]

    monkeypatch.setattr(cfg, "config", yaml.safe_load(stale))
    assert cfg.checkConfig() == ["reminderCheckHour"]


def test_cli_check_exit_codes(tmp_path, capsys):
    example = tmp_path / "config_example.yaml"
    example.write_text(EXAMPLE, encoding="utf8")
    config = tmp_path / "config.yaml"
    args = ["--config", str(config), "--example", str(example)]

    assert mig.main(["check"] + args) == 1  # no config.yaml at all
    config.write_text(LEGACY, encoding="utf8")
    assert mig.main(["check"] + args) == 1  # required keys missing

    assert mig.main(["apply"] + args) == 0
    assert mig.main(["check"] + args) == 0
    assert list(tmp_path.glob("config.yaml.*.bak")), "apply must leave a backup"

    # Only an optional key behind: the bot still starts, so don't fail a deploy
    # over it -- but --strict exists for anyone who wants that.
    config.write_text("\n".join(line for line in config.read_text(encoding="utf8").splitlines()
                                if "webhookSecret" not in line) + "\n", encoding="utf8")
    assert mig.main(["check"] + args) == 0
    assert mig.main(["check", "--strict"] + args) == 1


def test_cli_apply_creates_the_file_when_there_is_nothing_to_migrate(tmp_path):
    example = tmp_path / "config_example.yaml"
    example.write_text(EXAMPLE, encoding="utf8")
    config = tmp_path / "config.yaml"
    assert mig.main(["apply", "--config", str(config), "--example", str(example)]) == 0
    assert config.read_text(encoding="utf8") == EXAMPLE


def test_cli_reports_unparseable_yaml_instead_of_crashing(tmp_path):
    example = tmp_path / "config_example.yaml"
    example.write_text(EXAMPLE, encoding="utf8")
    config = tmp_path / "config.yaml"
    config.write_text("telegram:\n  token: 'unterminated\n", encoding="utf8")
    assert mig.main(["check", "--config", str(config), "--example", str(example)]) == 2
