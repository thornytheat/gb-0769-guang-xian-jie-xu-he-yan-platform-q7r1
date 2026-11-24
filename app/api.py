from flask import Blueprint, current_app, jsonify, request

from .errors import InspectionError
from .input import request_material
from .modules.archive.service import archive
from .modules.archive.storage import get, recent
from .modules.certificate.service import certify
from .modules.correction import storage as correction_storage
from .modules.correction.service import (archive_correction, record_line,
                                         recheck_correction, submit_correction)
from .pipeline import run

api = Blueprint('fiber_api', __name__, url_prefix='/api')


def calculate(stage):
    return run(request_material(), stage)[stage]


@api.post('/trace')
def trace():
    return calculate('trace')


@api.post('/calibration')
def calibration():
    return calculate('calibration')


@api.post('/pairs')
def pairs():
    return calculate('pairs')


@api.post('/loss')
def loss():
    return calculate('loss')


@api.post('/design')
def design():
    return calculate('design')


@api.post('/inspections')
def submit():
    report, fresh = archive(current_app.config['DATABASE'], request_material())
    return {'inspection_id':report['inspection_id'], 'created':fresh, 'record':report}, 201 if fresh else 200


@api.get('/inspections')
def list_inspections():
    return {'inspections':recent(current_app.config['DATABASE'])}


@api.get('/inspections/<identifier>')
def inspection_detail(identifier):
    return get(current_app.config['DATABASE'], identifier)


@api.get('/certificate/<identifier>')
def certificate(identifier):
    return certify(get(current_app.config['DATABASE'], identifier))


@api.get('/export/<identifier>')
def export(identifier):
    response = jsonify(certify(get(current_app.config['DATABASE'], identifier)))
    response.headers['Content-Disposition'] = f'attachment; filename="splice-{identifier[:12]}.json"'
    return response


def _line_codes():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise InspectionError('JSON_OBJECT_REQUIRED', status=400)
    codes = body.get('lines')
    if not isinstance(codes, list) or not codes or any(not isinstance(code, str) for code in codes):
        raise InspectionError('TWO_LINE_CODES_REQUIRED', status=400)
    return codes


@api.post('/lines/<line_code>/recordings')
def record_line_version(line_code):
    result = record_line(current_app.config['DATABASE'], line_code, request_material())
    return result, 201 if result['created'] else 200


@api.get('/lines')
def lines():
    with correction_storage.session(current_app.config['DATABASE']) as db:
        return {'lines': correction_storage.list_lines(db)}


@api.get('/lines/<line_code>')
def line_detail(line_code):
    with correction_storage.session(current_app.config['DATABASE']) as db:
        return correction_storage.get_line(db, line_code)


@api.post('/corrections')
def create_correction():
    document = submit_correction(current_app.config['DATABASE'], _line_codes())
    return document, 201


@api.get('/corrections')
def corrections():
    with correction_storage.session(current_app.config['DATABASE']) as db:
        return {'corrections': correction_storage.list_corrections(db)}


@api.get('/corrections/<identifier>')
def correction_detail(identifier):
    with correction_storage.session(current_app.config['DATABASE']) as db:
        return correction_storage.get_correction(db, identifier)


@api.post('/corrections/<identifier>/archive')
def correction_archive(identifier):
    return archive_correction(current_app.config['DATABASE'], identifier)


@api.post('/corrections/<identifier>/recheck')
def correction_recheck(identifier):
    return recheck_correction(current_app.config['DATABASE'], identifier)
