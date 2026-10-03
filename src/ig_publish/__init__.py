"""ig-publish: publish Instagram Stories and Reels through the official Instagram Graph API.

Use it from the command line (``ig-publish --help``) or as a library::

    from ig_publish import load_config, Publisher, Options

    cfg = load_config('ig-publish.toml')
    Publisher(cfg, Options(offline=True)).plan()
"""
from __future__ import annotations

__version__ = '0.1.0'

# The submodules import __version__ from here, so these imports come after it.
from .config import Config, load_config  # noqa: E402
from .errors import (  # noqa: E402
    EXIT_ERROR,
    EXIT_LATER,
    EXIT_OK,
    EXIT_USAGE,
    ConfigError,
    GraphError,
    IgPublishError,
    Stop,
    TemporaryStop,
    TokenError,
    UsageLimit,
)
from .publisher import Options, Publisher  # noqa: E402

__all__ = ['__version__', 'Config', 'load_config', 'Options', 'Publisher', 'IgPublishError', 'ConfigError',
           'GraphError', 'Stop', 'TemporaryStop', 'TokenError', 'UsageLimit', 'EXIT_OK', 'EXIT_ERROR', 'EXIT_USAGE',
           'EXIT_LATER']
