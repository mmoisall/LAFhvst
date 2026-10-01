"""구형 HVST → LAFhvst 이관 CLI 진입점.

사용 예:
    python tools/import_hvst.py --dry-run
    python tools/import_hvst.py --apply
    python tools/import_hvst.py --db "C:\\...\\hvst.db" --apply --include-secrets
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hvst_import.importer import run_cli  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run_cli())
