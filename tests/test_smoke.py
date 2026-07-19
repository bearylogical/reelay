def test_package_imports():
    # Importing these together would have failed on the old add<->delete cycle.
    import reelay.bot  # noqa: F401
    import reelay.delete  # noqa: F401
    import reelay.miniapp  # noqa: F401
    import reelay.digest  # noqa: F401
    import reelay.webhooks  # noqa: F401
    import reelay.conversation  # noqa: F401


def test_version_is_set():
    import reelay
    assert reelay.__version__
    assert reelay.__version__[0].isdigit()
