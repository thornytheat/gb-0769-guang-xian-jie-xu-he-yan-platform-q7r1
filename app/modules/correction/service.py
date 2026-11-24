from datetime import datetime, timezone
from time import perf_counter
from uuid import uuid4

from app.config import REMOTE_SOURCE
from app.errors import InspectionError
from app.modules.archive.service import build_record
from app.modules.archive.storage import publish, publish_with
from app.modules.certificate.service import certify
from app.pipeline import run
from . import storage


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def record_line(database, line_code, material):
    """Run one line and move its effective archive to the new version."""
    report, fresh = publish(database, build_record(material))
    with storage.session(database) as db:
        version = storage.record_line(db, line_code, report['inspection_id'], material, report['generated_at'])
    return {'line_code': line_code, 'version': version, 'inspection_id': report['inspection_id'],
            'created': fresh, 'record': report}


def corrected_materials(first_material, second_material):
    """Swap only the remote-end original; each line keeps its own A-end and design list."""
    corrected_first = {**first_material,
                       'traces': {**first_material['traces'],
                                  REMOTE_SOURCE: second_material['traces'][REMOTE_SOURCE]}}
    corrected_second = {**second_material,
                        'traces': {**second_material['traces'],
                                   REMOTE_SOURCE: first_material['traces'][REMOTE_SOURCE]}}
    return corrected_first, corrected_second


def recalc(line, side, material):
    """Recompute one corrected line and attach its issuance verdict and recalc counters."""
    started = perf_counter()
    results = run(material)
    report = build_record(material, results, started)
    verdict = certify(report)
    design = results['design']
    return {
        'line_code': line['line_code'], 'side': side,
        'pinned_version': line['version'], 'pinned_inspection_id': line['inspection_id'],
        'inspection_id': report['inspection_id'], 'approved': verdict['approved'],
        'blockers': verdict['blockers'],
        'recalc': {'passed': design['passed'], 'over_limit': design['over_limit'],
                   'missing': design['missing'], 'design_count': design['design_count']},
        'traces_sha256': report['traces_sha256'],
    }, verdict


def _verdict(snapshot):
    return {key: snapshot[key] for key in ('inspection_id', 'approved', 'blockers', 'recalc', 'traces_sha256')}


def submit_correction(database, line_codes):
    if len(line_codes) != 2 or line_codes[0] == line_codes[1]:
        raise InspectionError('CORRECTION_NEEDS_TWO_LINES', status=422)
    created_at = _now()
    correction_id = uuid4().hex
    with storage.session(database) as db:
        first = storage.get_line(db, line_codes[0])
        second = storage.get_line(db, line_codes[1])
        storage.lock_lines(db, (first['line_code'], second['line_code']), correction_id)
        first_material, second_material = corrected_materials(first['material'], second['material'])
        first_snapshot, _ = recalc(first, 0, first_material)
        second_snapshot, _ = recalc(second, 1, second_material)
        both_pass = first_snapshot['approved'] and second_snapshot['approved']
        document = {
            'correction_id': correction_id,
            'status': 'READY_TO_ARCHIVE' if both_pass else 'SUSPENDED',
            'remote_source': REMOTE_SOURCE,
            'lines': [first_snapshot, second_snapshot],
            'materials': {first['line_code']: first_material, second['line_code']: second_material},
            'pinned': {first['line_code']: first['version'], second['line_code']: second['version']},
            'verdicts': {first['line_code']: _verdict(first_snapshot),
                         second['line_code']: _verdict(second_snapshot)},
            'created_at': created_at, 'updated_at': created_at, 'archived_at': None,
            'return_reason': None,
        }
        storage.save_correction(db, document)
    return document


def _current_lines(db, document):
    return [storage.get_line(db, snapshot['line_code']) for snapshot in document['lines']]


def _is_stale(current, document):
    """True when any line recorded a new measurement after the correction was pinned."""
    return any(line['version'] != document['pinned'][line['line_code']] for line in current)


def _recompute(db, document, current):
    """Rebuild the swapped materials from the current archives and re-pin both lines."""
    materials = corrected_materials(current[0]['material'], current[1]['material'])
    resolved = []
    for line, material in zip(current, materials):
        snapshot = next(item for item in document['lines'] if item['line_code'] == line['line_code'])
        refreshed, _ = recalc(line, snapshot['side'], material)
        snapshot.clear()
        snapshot.update(refreshed)
        document['materials'][line['line_code']] = material
        document['pinned'][line['line_code']] = line['version']
        resolved.append((line, snapshot, material))
    document['verdicts'] = {snapshot['line_code']: _verdict(snapshot) for _, snapshot, _ in resolved}
    return resolved


def _mark_returned(db, document):
    document['status'] = 'RETURNED'
    document['return_reason'] = 'NEW_MEASUREMENT_PENDING'
    document['updated_at'] = _now()


def archive_correction(database, correction_id):
    """Move both lines to their new version together, or leave both on the old one."""
    with storage.session(database) as db:
        document = storage.get_correction(db, correction_id)
        if document['status'] == 'ARCHIVED':
            raise InspectionError('CORRECTION_NOT_ARCHIVABLE', source=document['status'], status=409)
        # 留档前任一线路录入新测量记录：纠正单退回重核，两条都不切换
        current = _current_lines(db, document)
        if _is_stale(current, document):
            _mark_returned(db, document)
            storage.update_correction(db, document)
            db.commit()
            raise InspectionError('LINE_MEASUREMENT_CHANGED', status=409)
        resolved = _recompute(db, document, current)
        # 一条合格、一条超限：两条都暂缓，留档保持旧版
        if not all(snapshot['approved'] for _, snapshot, _ in resolved):
            document['status'] = 'SUSPENDED'
            document['return_reason'] = None
            document['updated_at'] = _now()
            storage.update_correction(db, document)
            db.commit()
            raise InspectionError('CORRECTION_LINE_OVER_LIMIT', status=422)
        # 两条都合格才更新留档；同一事务内先后写入，中断则整体回滚，两线路同见旧版或同见新版
        # 条件占位先抢占 ARCHIVED，并发归档时落败者整体回滚，两线路版本不会被重复推进
        if not storage.claim_archived(db, correction_id):
            raise InspectionError('CORRECTION_NOT_ARCHIVABLE', source='ARCHIVED', status=409)
        archived_at = _now()
        for line, snapshot, material in resolved:
            report, _ = publish_with(db, build_record(material))
            version = storage.record_line(db, line['line_code'], report['inspection_id'], material, archived_at)
            snapshot['pinned_version'] = version
            snapshot['pinned_inspection_id'] = report['inspection_id']
        document['status'] = 'ARCHIVED'
        document['archived_at'] = archived_at
        document['updated_at'] = archived_at
        document['return_reason'] = None
        document['pinned'] = {snapshot['line_code']: snapshot['pinned_version'] for _, snapshot, _ in resolved}
        document['verdicts'] = {snapshot['line_code']: _verdict(snapshot) for _, snapshot, _ in resolved}
        storage.release_lines(db, tuple(document['pinned']))
        storage.update_correction(db, document)
        return document


def recheck_correction(database, correction_id):
    """Re-run the recalculation after a return or suspension.

    Rechecking confirms the current effective archives: swapped materials are
    rebuilt from them, both lines are recomputed together, and the pinned
    versions advance. The correction returns to READY_TO_ARCHIVE only when both
    lines pass, otherwise it stays SUSPENDED with the fresh per-line verdicts.
    """
    with storage.session(database) as db:
        document = storage.get_correction(db, correction_id)
        if document['status'] not in ('RETURNED', 'SUSPENDED'):
            raise InspectionError('CORRECTION_NOT_RECHECKABLE', source=document['status'], status=409)
        current = _current_lines(db, document)
        # 重核即验收员确认当前留档：以当前版本重新交换远端原件、复算并推进钉版
        _recompute(db, document, current)
        document['updated_at'] = _now()
        if all(snapshot['approved'] for snapshot in document['lines']):
            document['status'] = 'READY_TO_ARCHIVE'
            document['return_reason'] = None
        else:
            document['status'] = 'SUSPENDED'
            document['return_reason'] = None
        storage.update_correction(db, document)
        return document
