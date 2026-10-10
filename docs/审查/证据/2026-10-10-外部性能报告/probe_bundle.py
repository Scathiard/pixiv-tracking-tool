"""Read the delivered EXE and run only an empty in-memory SQLite query."""
import ctypes
import hashlib
import json
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader

OUT = Path(__file__).resolve().parent
ROOT = next(parent for parent in OUT.parents if
            (parent / "release/v0.2.0/build-20261008.1/PixivTracker_v0.2.0_build20261008.1.exe").is_file())
EXE = ROOT / "release/v0.2.0/build-20261008.1/PixivTracker_v0.2.0_build20261008.1.exe"
dll = OUT / "baseline-sqlite3.dll"
data = CArchiveReader(str(EXE)).extract("sqlite3.dll")
if dll.exists():
    assert dll.read_bytes() == data
else:
    dll.write_bytes(data)
api = ctypes.CDLL(str(dll))
api.sqlite3_libversion.restype = ctypes.c_char_p
api.sqlite3_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
api.sqlite3_exec.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_char_p)]
api.sqlite3_close.argtypes = [ctypes.c_void_p]
api.sqlite3_free.argtypes = [ctypes.c_void_p]
connection = ctypes.c_void_p()
assert api.sqlite3_initialize() == 0
assert api.sqlite3_open(b":memory:", ctypes.byref(connection)) == 0
query = b"""
CREATE TABLE operations(id INTEGER, state TEXT);
CREATE TABLE operation_items(id INTEGER, operation_id INTEGER, state TEXT, position INTEGER);
INSERT INTO operations VALUES(1,'running');
INSERT INTO operation_items VALUES(1,1,'pending',0),(2,1,'running',1);
SELECT o.id, (SELECT i.id FROM operation_items i WHERE i.operation_id=o.id
 ORDER BY CASE WHEN o.state='running' AND i.state='running' THEN 0 ELSE 1 END,
 i.position LIMIT 1) FROM operations o;
"""
error = ctypes.c_char_p()
try:
    code = api.sqlite3_exec(connection, query, None, None, ctypes.byref(error))
    result = {"exe": str(EXE), "exe_sha256": hashlib.sha256(EXE.read_bytes()).hexdigest(),
              "sqlite": api.sqlite3_libversion().decode(), "sqlite_dll_sha256": hashlib.sha256(data).hexdigest(),
              "database": ":memory:", "return_code": code,
              "error": error.value.decode() if error.value else None,
              "real_library_used": False, "real_account_used": False}
    (OUT / "bundle-probe.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
finally:
    if error.value:
        api.sqlite3_free(error)
    api.sqlite3_close(connection)
