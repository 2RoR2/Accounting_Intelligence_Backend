"""Non-destructive write/read check beside this starter. No dependencies."""
from pathlib import Path
import shutil
import tempfile

root = Path(__file__).resolve().parents[2]
print('Project storage root:', root)
usage = shutil.disk_usage(root)
print(f'Free space: {usage.free / (1024 ** 3):.1f} GiB')
with tempfile.TemporaryDirectory(prefix='ai-storage-probe-', dir=root) as folder:
    probe = Path(folder) / 'probe.txt'
    probe.write_text('Synthetic storage check', encoding='utf-8')
    assert probe.read_text(encoding='utf-8') == 'Synthetic storage check'
print('PASS: temporary file written, read and removed.')
print('Confirm the root is the SSD. This is not a speed, health or compliance test.')
