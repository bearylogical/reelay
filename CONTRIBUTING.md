# Contributing to Reelay

Thanks for your interest! Reelay is a single-process Python package
(`python -m reelay`) backed by one SQLite file.

## Dev setup

```bash
python -m venv .venv && . .venv/bin/activate
make install            # pip install -e ".[test]"
make config-init        # config_example.yaml -> config.yaml, fill in telegram.token
make test               # run the test suite
make run                # run the bot
```

Tests fall back to `config_example.yaml` automatically, so `pytest` works
without a `config.yaml`. `make` with no target lists everything.

## Guidelines

- **Keep it importable.** Modules use relative imports and must not
  reintroduce import cycles — shared conversation helpers live in
  `reelay/conversation.py` for exactly this reason.
- **Add tests.** New behaviour in `db`, `overseerr`, `miniapp`, `webhooks`,
  or `digest` should come with a test under `tests/`. CI runs `pytest` and a
  syntax check on every push and PR.
- **User-facing strings** go through `i18n.t("reelay....")` and are defined in
  `translations/reelay.en-us.yml`.
- **New config** belongs in both `config_example.yaml` and the
  `DEFAULT_SETTINGS` dict in `reelay/definitions.py`. Comment it in the example
  — `make config-migrate` copies that comment into existing installs along with
  the key, and it is the only explanation they get. If the bot reads the key
  defensively and simply leaves a feature off when it's absent, add its path to
  `OPTIONAL_KEYS` (also in `definitions.py`) so existing configs aren't told
  they're broken. Then run `make config-check` against a real `config.yaml` to
  see what an operator will see.

## Versioning

Versions are managed automatically. Merges to `main` bump a semver tag
(`vX.Y.Z`) based on [Conventional Commits](https://www.conventionalcommits.org/)
(`fix:` → patch, `feat:` → minor, `feat!:`/`BREAKING CHANGE` → major; default
patch) and write the `VERSION` files. Please prefix commits accordingly.

## License

By contributing you agree your contributions are licensed under the
[MIT License](LICENSE).
