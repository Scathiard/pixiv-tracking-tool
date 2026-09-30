"""One persisted catalogue-change counter; callers share their current transaction."""
from .models import ApplicationState

KEY = "catalogue_revision"


def catalogue_revision(session) -> int:
    record = session.get(ApplicationState, KEY)
    value = record.value if record is not None else 0
    return value if type(value) is int and value >= 0 else 0


def bump_catalogue_revision(session) -> int:
    value = catalogue_revision(session) + 1
    record = session.get(ApplicationState, KEY)
    if record is None:
        session.add(ApplicationState(key=KEY, value=value))
    else:
        record.value = value
    return value
