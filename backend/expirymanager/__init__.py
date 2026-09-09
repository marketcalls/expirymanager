"""ExpiryManager: a zero-config local platform for Fyers expired F&O history.

Kept import-light on purpose. Importing this package must not open a database, read a key or
touch the data directory, because the console entry point sets the process umask before any of
that is allowed to happen.
"""

from expirymanager.version import APP_NAME, __version__

__all__ = ["APP_NAME", "__version__"]
