"""``python -m bobanana6`` 的入口。真正的工作在 ``cli.py``。"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
