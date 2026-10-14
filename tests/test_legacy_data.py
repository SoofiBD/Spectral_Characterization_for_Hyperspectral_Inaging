"""Read-only check against the October 12 audit baseline, not a historical authenticity claim."""
import hashlib
import json
from pathlib import Path
BASE=Path(__file__).resolve().parents[1]
records=json.loads((BASE/'tests/legacy_npz_sha256.json').read_text())
if not (BASE/'data').exists() or not any((BASE/'data').glob('run_20261007*')):
    print('SKIP: private October 7 measurements are not present in this checkout')
else:
    for relative, expected in records.items():
        with (BASE/relative).open('rb') as f:
            assert hashlib.file_digest(f,'sha256').hexdigest()==expected,relative
    print(f'LEGACY DATA UNCHANGED: {len(records)} NPZ files match the audit baseline')
