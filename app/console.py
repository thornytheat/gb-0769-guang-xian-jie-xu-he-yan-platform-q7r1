from flask import Blueprint, abort, current_app, render_template, request

from .config import SOURCES, SPEC
from .errors import InspectionError
from .input import sample
from .modules.archive.service import archive
from .modules.archive.storage import get, recent
from .modules.certificate.service import certify
from .modules.correction import storage as correction_storage
from .modules.correction.service import (archive_correction, record_line,
                                         recheck_correction, submit_correction)
from .pipeline import run
from .modules.calibration.contract import FIELDS
from .views import CODES, LABELS, half_mm_metres, metres, trace_frames

console = Blueprint('console', __name__)
console.add_app_template_filter(metres, 'metres')
console.add_app_template_filter(half_mm_metres, 'half_mm_metres')

CORRECTION_STATUS = {
    'READY_TO_ARCHIVE': '待留档（两条均合格）', 'SUSPENDED': '暂缓签发（存在超限或缺证）',
    'RETURNED': '退回重核（留档前录入新测量）', 'ARCHIVED': '已留档生效',
}


def material(mode):
    if mode == 'custom':
        return {'traces':{key:request.form.get(key,'') for key in SOURCES},
                'settings':{key:request.form.get(key,'') for key in FIELDS}}
    return sample(mode)


@console.route('/', methods=['GET','POST'])
@console.route('/inspect/<section>', methods=['GET','POST'])
def view(section='design'):
    if section not in LABELS:
        abort(404)
    mode = request.values.get('sample','dirty')
    database = current_app.config['DATABASE']
    evidence, frames, entries, data = None, {}, [], {}
    issued, fresh, error, sources = None, False, None, {}
    line_rows, correction_rows, correction, correction_error = [], [], None, None
    try:
        sources = material(mode)
        if section == 'correction':
            with correction_storage.session(database) as db:
                line_rows = correction_storage.list_lines(db)
                correction_rows = correction_storage.list_corrections(db)
            if request.method == 'POST':
                action = request.form.get('action')
                if action == 'record':
                    issued = record_line(database, request.form['line_code'], sources)
                elif action == 'submit':
                    codes = [request.form['first_line'], request.form['second_line']]
                    correction = submit_correction(database, codes)
                elif action in ('archive', 'recheck'):
                    identifier = request.form['correction_id']
                    correction = (archive_correction if action == 'archive' else recheck_correction)(database, identifier)
            identifier = request.args.get('correction')
            if not correction and identifier:
                with correction_storage.session(database) as db:
                    correction = correction_storage.get_correction(db, identifier)
        elif section in ('archive','certificate'):
            if section == 'archive' and request.method == 'POST':
                issued, fresh = archive(database, sources)
            entries = recent(database)
            identifier = request.args.get('inspection') or (entries[0]['inspection_id'] if entries else None)
            if section == 'certificate' and identifier:
                evidence = certify(get(database, identifier))
        else:
            data = run(sources, section)
            if 'calibration' in data:
                frames = trace_frames(data['calibration'])
    except InspectionError as exc:
        if section == 'correction':
            correction_error = exc.code
        else:
            error = exc.code
    return render_template('console.html', section=section, labels=LABELS, codes=CODES, spec=SPEC, mode=mode,
                           data=data, frames=frames, error=error, materials=sources, entries=entries,
                           evidence=evidence, issued=issued, fresh=fresh, fields=FIELDS,
                           line_rows=line_rows, correction_rows=correction_rows,
                           correction=correction, correction_error=correction_error,
                           correction_status=CORRECTION_STATUS)
