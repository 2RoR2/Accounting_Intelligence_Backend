"""Native WSL metadata transport; identical fail-closed manifest assertions.

Only the transport changes from Windows interop to an explicit local Unix
socket. No context fallback, caching, Docker mutations or assertion changes.
"""
import subprocess
from tests import p4a_support as h

def native_metadata(*args):
    if not args or args[0] not in ('inspect','volume','network'):
        raise h.IsolationError('Only read-only metadata inspection allowed')
    try:
        return subprocess.run(['/usr/bin/docker','--host','unix:///var/run/docker.sock',*args],
            check=True,capture_output=True,text=True,timeout=15).stdout
    except Exception:
        raise h.IsolationError('Exact native Docker metadata transport unavailable') from None

def enable():
    h.docker=native_metadata
