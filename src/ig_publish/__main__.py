"""``python -m ig_publish`` (the scheduler uses this to start the publisher)."""
import sys

from .cli import main

sys.exit(main())
