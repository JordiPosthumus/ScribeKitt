#!/usr/bin/env python3
"""Thin entrypoint for the ML JSON-RPC daemon.

The core logic lives in the `ml` package; this file stays bundled as the
resource entrypoint the Swift app launches.
"""

from ml.rpc import main


if __name__ == "__main__":
    # The parent closing stdin is a quit signal, not a request to finish queued
    # inference. The listener has already drained in main's finally block.
    import os
    os._exit(main())
