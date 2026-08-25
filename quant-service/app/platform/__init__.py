"""Framework-light contracts shared across bounded application domains.

This package must not import routers, application composition, provider
transports, or database repositories.  It is deliberately safe for pure
contract tests and for maintenance agents that need an ownership map.
"""
