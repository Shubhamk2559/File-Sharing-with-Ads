"""Entry point kept for discoverability:  python migrations/001_smart_ads.py  (same as python -m ads.migrate)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ads.migrate import main  # noqa: E402

if __name__ == "__main__":
    main()
