from time import perf_counter

from app.pipeline import run
from .identity import record
from .storage import publish


def build_record(materials, results=None, started=None):
    started = perf_counter() if started is None else started
    if results is None:
        results = run(materials)
    return record(materials, results, started)


def archive(database, materials):
    started = perf_counter()
    return publish(database, build_record(materials, started=started))
