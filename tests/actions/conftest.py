"""Skip all action tests when tomllib is not available (Python < 3.11)."""

import sys

collect_ignore_glob = ["test_*.py"] if sys.version_info < (3, 11) else []
