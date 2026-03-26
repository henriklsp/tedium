def pytest_collection_modifyitems(items):
    """Restrict BDD-style collection to class methods only.

    python_functions = [!_]* would otherwise treat module-level imports
    and helper functions as test candidates. This hook keeps only items
    that belong to a class (Describe* methods) or start with test_ (plain
    module-level test functions).
    """
    _lifecycle = {"setup_method", "teardown_method", "setup_class", "teardown_class",
                  "setup", "teardown"}
    items[:] = [
        item for item in items
        if item.name not in _lifecycle
        and (getattr(item, "cls", None) is not None or item.name.startswith("test_"))
    ]
