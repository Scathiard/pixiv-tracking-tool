"""Synthetic pending refresh / deliberate write collision, no real account."""
import json
from pathlib import Path
import sys
import time
from threading import Event
from uuid import uuid4

sys.path.insert(0, str(Path.cwd() / 'app'))
from src.core.bookmarks import BookmarkService
from src.core.database import ArchiveDatabase
from src.core.models import Artist, Work

folder = Path(__file__).parent.resolve()
database = ArchiveDatabase.create(folder / 'synthetic-bookmarks.db')
with database.write() as session:
    session.add(Work(id=1, pixiv_illust_id='100', title='Synthetic',
                     artist=Artist(id=1, pixiv_user_id='200', name='Synthetic')))

class Remote:
    def __init__(self):
        self.read_started, self.release = Event(), Event()
        self.writes = 0
    def get_account_id(self): return 'synthetic'
    def read_bookmark_bound(self, _id, account):
        assert account == 'synthetic'
        self.read_started.set()
        assert self.release.wait(5)
        return False
    def write_bookmark(self, *_args): self.writes += 1

remote = Remote()
service = BookmarkService(database, remote, current_account=lambda: 'synthetic')
service.start()
try:
    refresh = service.refresh(1)
    assert remote.read_started.wait(5)
    result = service.submit(1, True, str(uuid4()), expected_account_id='synthetic')
    remote.release.set()
    for _ in range(100):
        terminal = service.operation(result['operation_id'])
        if terminal['status'] != 'pending': break
        time.sleep(.01)
    evidence = {'same_operation':result['operation_id'] == refresh['operation_id'],
                'returned_kind':terminal['kind'], 'returned_desired':terminal['desired'],
                'terminal':terminal['status'], 'fact':terminal['value'], 'writes':remote.writes}
    print(json.dumps(evidence))
    assert remote.writes == 1, 'write intent is swallowed by existing refresh; no bookmark write was sent'
finally:
    remote.release.set()
    assert service.stop()
    database.close()
