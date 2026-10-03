"""Conditional local media responses, called only after normal access checks."""
from email.utils import parsedate_to_datetime
from hashlib import sha256

from fastapi.responses import FileResponse, Response


def local_media_response(request, path):
    stat = path.stat()
    response = FileResponse(path, stat_result=stat)
    version = f"{stat.st_mtime_ns}:{stat.st_size}:{stat.st_ino}"
    etag = '"' + sha256(version.encode()).hexdigest() + '"'
    response.headers["etag"] = etag
    response.headers["cache-control"] = "private, max-age=0, must-revalidate"
    none_match = request.headers.get("if-none-match")
    matched = False
    if none_match is not None:
        matched = any(value.strip().removeprefix("W/") in (etag, "*") for value in none_match.split(","))
    elif request.headers.get("if-modified-since"):
        try:
            matched = int(stat.st_mtime) <= parsedate_to_datetime(request.headers["if-modified-since"]).timestamp()
        except (ValueError, TypeError, OverflowError):
            pass
    if matched:
        return Response(status_code=304, headers={name: response.headers[name]
            for name in ("etag", "last-modified", "cache-control")})
    return response
