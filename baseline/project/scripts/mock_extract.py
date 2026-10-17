"""Emit a synthetic fixture. This does NOT run Docling, Qwen or real extraction."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('case', choices=['normal', 'missing', 'unsupported'])
args = parser.parse_args()
root = Path(__file__).resolve().parents[2]
result = json.loads((root / 'project' / 'fixtures' / (args.case + '.json')).read_text(encoding='utf-8'))
if result.get('mode') != 'mock':
    raise ValueError('This command accepts mock fixtures only')
run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8]
result['run_id'] = run_id
folder = root / 'runs' / run_id
folder.mkdir(parents=True, exist_ok=False)
(folder / 'result.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
print('MOCK ONLY — no document read and no AI called.')
print('Saved:', folder / 'result.json')
