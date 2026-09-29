"""Shared check for the browser read routes (split, part 2)."""
from fastapi import params

from api.auth import require_reader


def assert_reader_only_get(routes):
    """Each route is GET-only and carries require_reader (no public exemption)."""
    routes = list(routes)
    assert routes
    for route in routes:
        assert route.methods == {"GET"}, route.path
        calls = {d.dependency for d in route.dependencies if isinstance(d, params.Depends)}
        assert require_reader in calls, route.path
