"""Reuse httplib2's SSL context across Google API connections.

``googleapiclient`` builds a new ``httplib2.Http`` for every service this
server constructs, and ``httplib2`` builds a fresh ``ssl.SSLContext`` for
every HTTPS connection: a new context object plus a full read of the
certifi bundle. Measured at about 18 ms of CPU per connection, paid on
every tool call because each call builds and closes its own service.

An ``SSLContext`` is immutable once configured and safe to share between
connections and threads, so one context per distinct set of arguments is
enough. This module wraps ``httplib2._build_ssl_context`` in an LRU cache.
The wrapper falls back to the original builder when an argument is not
hashable (never the case for httplib2's own calls, which pass strings,
bools and ``None``), so behaviour is unchanged in every case the cache
cannot serve.
"""

from __future__ import annotations

import functools
import logging
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_installed = False
_CACHE_SIZE = 16


def install_httplib2_ssl_context_cache() -> bool:
    """Patch ``httplib2._build_ssl_context`` with a cached version.

    Idempotent. Returns ``True`` when the cache is active after the call
    (installed now or earlier) and ``False`` when httplib2 is unavailable.
    """
    global _installed
    with _lock:
        if _installed:
            return True
        try:
            import httplib2
        except ImportError:  # pragma: no cover - httplib2 is a hard dependency
            logger.warning("httplib2 not importable; SSL context cache not installed")
            return False

        original = httplib2._build_ssl_context
        cached = functools.lru_cache(maxsize=_CACHE_SIZE)(original)

        @functools.wraps(original)
        def _build_ssl_context_cached(*args, **kwargs):
            try:
                return cached(*args, **kwargs)
            except TypeError:
                # Unhashable argument (not something httplib2 itself does):
                # build an uncached context exactly as before.
                return original(*args, **kwargs)

        _build_ssl_context_cached.cache_info = cached.cache_info  # type: ignore[attr-defined]
        _build_ssl_context_cached.cache_clear = cached.cache_clear  # type: ignore[attr-defined]
        _build_ssl_context_cached.__wrapped_original__ = original  # type: ignore[attr-defined]
        httplib2._build_ssl_context = _build_ssl_context_cached
        _installed = True
        logger.debug("httplib2 SSL context cache installed (maxsize=%d)", _CACHE_SIZE)
        return True


def is_installed() -> bool:
    return _installed
