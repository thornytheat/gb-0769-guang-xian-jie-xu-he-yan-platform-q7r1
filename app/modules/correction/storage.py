import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from app.errors import InspectionError

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS inspections (
        id TEXT UNIQUE NOT NULL, document BLOB NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS lines (
        code TEXT UNIQUE NOT NULL,
        version INTEGER NOT NULL,
        inspection_id TEXT NOT NULL,
        material TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS line_versions (
        line_code TEXT NOT NULL,
        version INTEGER NOT NULL,
        inspection_id TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        document TEXT NOT NULL,
        PRIMARY KEY (line_code, version)
    )""",
    """CREATE TABLE IF NOT EXISTS corrections (
        id TEXT UNIQUE NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        document TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS line_locks (
        line_code TEXT PRIMARY KEY,
        correction_id TEXT NOT NULL
    )""",
)


@contextmanager
def session(database):
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database, timeout=15)) as connection, connection:
        for statement in SCHEMA:
            connection.execute(statement)
        yield connection


def dump(document):
    return json.dumps(document, ensure_ascii=False, sort_keys=True)


def load(text):
    return json.loads(text)


def lock_lines(db, line_codes, correction_id):
    """Acquire an exclusive lock on every line; raise on a single conflict."""
    for code in sorted(line_codes):
        try:
            db.execute('INSERT INTO line_locks VALUES (?, ?)', (code, correction_id))
        except sqlite3.IntegrityError:
            held = db.execute('SELECT correction_id FROM line_locks WHERE line_code=?', (code,)).fetchone()
            raise InspectionError('CORRECTION_LINE_BUSY', source=held[0] if held else code, status=409)


def release_lines(db, line_codes):
    db.executemany('DELETE FROM line_locks WHERE line_code=?', ((code,) for code in line_codes))


def get_line(db, code):
    row = db.execute('SELECT version, inspection_id, material, updated_at FROM lines WHERE code=?', (code,)).fetchone()
    if not row:
        raise InspectionError('LINE_NOT_FOUND', source=code, status=404)
    return {'line_code': code, 'version': row[0], 'inspection_id': row[1],
            'material': load(row[2]), 'updated_at': row[3]}


def list_lines(db):
    rows = db.execute('SELECT code, version, inspection_id, updated_at FROM lines ORDER BY code').fetchall()
    return [{'line_code': code, 'version': version, 'inspection_id': inspection_id, 'updated_at': updated_at}
            for code, version, inspection_id, updated_at in rows]


def get_correction(db, identifier):
    row = db.execute('SELECT document FROM corrections WHERE id=?', (identifier,)).fetchone()
    if not row:
        raise InspectionError('CORRECTION_NOT_FOUND', source=identifier, status=404)
    return load(row[0])


def list_corrections(db):
    rows = db.execute('SELECT document FROM corrections ORDER BY rowid DESC LIMIT 50').fetchall()
    return [load(text) for (text,) in rows]


def save_correction(db, document):
    db.execute('INSERT INTO corrections VALUES (?, ?, ?, ?, ?)',
               (document['correction_id'], document['status'], document['created_at'],
                document['updated_at'], dump(document)))


def update_correction(db, document):
    db.execute('UPDATE corrections SET status=?, updated_at=?, document=? WHERE id=?',
               (document['status'], document['updated_at'], dump(document), document['correction_id']))


def claim_archived(db, correction_id):
    """Compare-and-swap guard: only one transaction may move a correction to ARCHIVED."""
    rowcount = db.execute(
        "UPDATE corrections SET status='ARCHIVED' WHERE id=? AND status!='ARCHIVED'",
        (correction_id,)).rowcount
    return rowcount == 1


def record_line(db, code, inspection_id, material, recorded_at):
    """Append a new line version and move the effective pointer; returns the new version number."""
    current = db.execute('SELECT version FROM lines WHERE code=?', (code,)).fetchone()
    version = (current[0] + 1) if current else 1
    document = {'line_code': code, 'version': version, 'inspection_id': inspection_id,
                'recorded_at': recorded_at, 'material': material}
    db.execute('INSERT INTO line_versions (line_code, version, inspection_id, recorded_at, document) VALUES (?,?,?,?,?)',
               (code, version, inspection_id, recorded_at, dump(document)))
    db.execute(
        'INSERT INTO lines (code, version, inspection_id, material, updated_at) VALUES (?,?,?,?,?) '
        'ON CONFLICT(code) DO UPDATE SET version=excluded.version, inspection_id=excluded.inspection_id, '
        'material=excluded.material, updated_at=excluded.updated_at',
        (code, version, inspection_id, dump(material), recorded_at))
    return version
