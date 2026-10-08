"""Same real project collection, with configuration and temporary files isolated."""
import faulthandler
faulthandler.enable(all_threads=True)
import os
from pathlib import Path
import runpy
import socket
import sys
import tempfile

out = Path(sys.argv[1]).resolve()
base = Path(__file__).resolve().parent
if out.parent != base or out.name not in ('collect-01', 'collect-02'):
    raise RuntimeError('Only this diagnosis directory is allowed')
os.environ['DATA_ROOT'] = str(out / 'synthetic-config')
os.environ['TEMP'] = os.environ['TMP'] = str(out / 'temp')
tempfile.tempdir = str(out / 'temp')
os.environ['PIXIV_REFRESH_TOKEN'] = ''


def no_network(*args, **kwargs):
    raise RuntimeError('Network calls are forbidden in this collection-only diagnostic')


socket.socket.connect = no_network
socket.socket.connect_ex = no_network
socket.socket.bind = no_network
socket.socket.listen = no_network
socket.getaddrinfo = no_network
import dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False
app = base.parents[2] / '.worktrees/v0.2.0-build20261004.3/app'
sys.path.insert(0, str(app))
sys.argv = ['pytest', '--collect-only', '-q', 'tests', '--basetemp=' + str(out / 'pytest'),
            '-o', 'cache_dir=' + str(out / 'pytest-cache')]
runpy.run_module('pytest', run_name='__main__')
