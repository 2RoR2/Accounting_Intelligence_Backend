"""SC2 manual-effort study: the same public documents typed manually or reviewed with AccordAI's extraction.

Tasks are labelled public benchmark files only; nothing here touches tenant documents or a model.
Each completed task is stored as one audited `effort_study.session` event (existing audit_logs table,
no schema change). The reduction is computed only from completed human sessions and is reported as
valid only when participants have done both modes in both orders; nothing is estimated from latency.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from decimal import InvalidOperation
from pathlib import Path

from main import *  # noqa: F401,F403  (require_workspace_user, connect, HTTPException, FileResponse, Response, Request)
from pydantic import BaseModel, Field

from ai.evaluation.effort_study_inputs import SETS, StudyUnavailable, load_samples, study_prefill

ROOT = Path(__file__).resolve().parents[3]
ACTION = 'effort_study.session'
STUDY_VERSION = 'effort-study/1'
MIN_PAIRED_PARTICIPANTS = 2


class SessionInput(BaseModel):
    participant: str = Field(min_length=1, max_length=40)
    sampleFile: str = Field(min_length=1, max_length=40)
    mode: str
    activeSeconds: float = Field(ge=0, le=7200)
    idleSeconds: float = Field(ge=0, le=7200)
    pausedSeconds: float = Field(ge=0, le=7200)
    values: dict[str, str | None]
    editedKeys: list[str] = []
    startedAt: str
    completedAt: str


def _samples():
    return load_samples(ROOT)


def _fields(sample):
    """Labelled header fields a person can type. Line items are not part of this study version."""
    return [label for label in sample.fields if label.label_state == 'PRESENT' and label.annotation.verification_state != 'UNVERIFIED']


def _ai_inputs():
    """AccordAI's measured extraction for the study files (see ai/evaluation/effort_study_inputs.py)."""
    try:
        return study_prefill(_samples())
    except StudyUnavailable as exc:
        raise HTTPException(503, f'The AI-assisted part of the manual-effort study is unavailable on this server: {exc}. '
                                 'Install ai/evaluation/effort_study_prefill.json or set EFFORT_STUDY_RUN_DIR to a measured '
                                 'scripts/saic_evaluate.py run. Recorded sessions and results are unaffected.') from exc


def _order(participant):
    """Counterbalanced by participant number: odd codes (P01, P03 ...) type set A manually first, even codes use AI on
    set A first and type set B. A code without a number falls back to a stable digest."""
    import re
    digits = re.findall(r'\d+', participant)
    odd = int(digits[-1]) % 2 == 1 if digits else int(hashlib.sha256(participant.strip().lower().encode()).hexdigest(), 16) % 2 == 1
    return [('manual', 'A'), ('ai_assisted', 'B')] if odd else [('ai_assisted', 'A'), ('manual', 'B')]


def study_tasks(request: Request, participant: str):
    require_workspace_user(request, {'accountant', 'local_admin'})
    if not participant.strip():
        raise HTTPException(422, 'A participant code is required.')
    samples = _samples()
    run_name, results = _ai_inputs()
    tasks = []
    for mode, set_name in _order(participant):
        for stem in SETS[set_name]:
            sample = samples[stem]
            prefill = None
            if mode == 'ai_assisted':
                prefill = results[stem]
            tasks.append({'sampleFile': stem, 'kind': sample.document_type, 'mode': mode, 'set': set_name,
                          'file': f'/api/effort-study/files/{stem}',
                          'fields': [{'key': f.field_key, 'type': f.value_type} for f in _fields(sample)], 'prefill': prefill})
    return {'version': STUDY_VERSION, 'participant': participant.strip(), 'aiRun': run_name, 'tasks': tasks,
            'lineItems': 'Not included in this study version.'}


def study_file(sample_file: str, request: Request):
    require_workspace_user(request, {'accountant', 'local_admin'})
    sample = _samples().get(sample_file)
    if not sample or sample_file not in {stem for stems in SETS.values() for stem in stems}:
        raise HTTPException(404, 'Not a study document.')
    path = (ROOT / 'ai' / sample.source.relative_path).resolve()
    if not path.is_relative_to((ROOT / 'ai' / 'assets').resolve()) or not path.is_file():
        raise HTTPException(404, 'Study document not found.')
    return FileResponse(path)


def _correct(label, value):
    from ai.evaluation.field_accuracy import _same_value
    from ai.evaluation.address_policy import ADDRESS_FIELDS, same_address
    if value is None or not str(value).strip():
        return False
    expected = label.normalized_value if label.normalized_value is not None else label.value
    actual = str(value).strip()
    try:
        if _same_value(label.value_type, expected, actual):
            return True
    except (InvalidOperation, ValueError, TypeError):
        return False
    return label.field_key in ADDRESS_FIELDS and label.value_type == 'text' and same_address(expected, actual)


def record_session(data: SessionInput, request: Request):
    user = require_workspace_user(request, {'accountant', 'local_admin'})
    if data.mode not in {'manual', 'ai_assisted'}:
        raise HTTPException(422, 'Mode must be manual or ai_assisted.')
    sample = _samples().get(data.sampleFile)
    order = _order(data.participant)
    expected_mode = {stem: mode for mode, set_name in order for stem in SETS[set_name]}
    if not sample or expected_mode.get(data.sampleFile) != data.mode:
        raise HTTPException(422, 'This document is not assigned to this participant in this mode.')
    fields = _fields(sample)
    prefill, ai_inputs = {}, None
    if data.mode == 'ai_assisted':
        ai_inputs, results = _ai_inputs()  # Never score an AI-assisted session against an empty prefill.
        prefill = results[data.sampleFile]
    keys = [f.field_key for f in fields]
    filled = {k: (data.values.get(k) or '').strip() for k in keys}
    if any(not filled[k] for k in keys):
        raise HTTPException(422, 'Complete every field before submitting; use the document to fill it.')
    edited = set(data.editedKeys) & set(keys)
    ai_accepted = sum(1 for k in keys if prefill.get(k) and k not in edited and filled[k] == prefill[k])
    ai_corrected = sum(1 for k in keys if prefill.get(k) and (k in edited or filled[k] != prefill[k]))
    typed = len(keys) if data.mode == 'manual' else sum(1 for k in keys if not prefill.get(k) or k in edited or filled[k] != prefill.get(k))
    correct = sum(1 for f in fields if _correct(f, filled[f.field_key]))
    details = {'version': STUDY_VERSION, 'participant': data.participant.strip(), 'sampleFile': data.sampleFile, 'kind': sample.document_type,
               'mode': data.mode, 'order': [mode for mode, _ in order].index(data.mode) + 1,
               'activeSeconds': round(data.activeSeconds, 1), 'idleSeconds': round(data.idleSeconds, 1), 'pausedSeconds': round(data.pausedSeconds, 1),
               'fieldsTotal': len(keys), 'fieldsTyped': typed, 'aiAccepted': ai_accepted, 'aiCorrected': ai_corrected,
               'correct': correct, 'quality': round(correct / len(keys), 3) if keys else None,
               'startedAt': data.startedAt, 'completedAt': data.completedAt,
               **({'aiInputs': ai_inputs} if ai_inputs else {})}
    with connect() as db:
        db.execute("""INSERT INTO audit_logs(tenant_id,user_id,user_name,role,action,resource,details)
            VALUES (%s,%s,%s,%s,%s,%s,%s)""", (user['tenant_id'], user['id'], user.get('name', 'Workspace user'), user['role'],
            ACTION, 'effort-study:' + data.sampleFile, json.dumps(details)))
    return details


def _sessions(user):
    with connect() as db:
        rows = db.execute("""SELECT details, created_at, user_name FROM audit_logs
            WHERE tenant_id=%s AND action=%s ORDER BY created_at""", (user['tenant_id'], ACTION)).fetchall()
    out = []
    for row in rows:
        details = row['details'] if isinstance(row['details'], dict) else json.loads(row['details'])
        out.append({**details, 'recordedAt': str(row['created_at']), 'recordedBy': row['user_name']})
    return out


def summarise(sessions):
    """Reduction from paired participants only. Valid when both orders are represented and enough people took part."""
    by_participant = {}
    for s in sessions:
        by_participant.setdefault(s['participant'], {}).setdefault(s['mode'], {})[s['sampleFile']] = s  # Latest per task.
    paired = []
    for participant, modes in by_participant.items():
        manual, assisted = list(modes.get('manual', {}).values()), list(modes.get('ai_assisted', {}).values())
        if len(manual) >= len(SETS['A']) and len(assisted) >= len(SETS['A']):
            m, a = sum(s['activeSeconds'] for s in manual), sum(s['activeSeconds'] for s in assisted)
            paired.append({'participant': participant, 'manualSeconds': round(m, 1), 'assistedSeconds': round(a, 1),
                           'manualFirst': manual[0]['order'] == 1,
                           'manualQuality': round(sum(s['correct'] for s in manual) / max(1, sum(s['fieldsTotal'] for s in manual)), 3),
                           'assistedQuality': round(sum(s['correct'] for s in assisted) / max(1, sum(s['fieldsTotal'] for s in assisted)), 3),
                           'reduction': round((m - a) / m * 100, 1) if m > 0 else None})
    manual_total = sum(p['manualSeconds'] for p in paired)
    reduction = round((manual_total - sum(p['assistedSeconds'] for p in paired)) / manual_total * 100, 1) if manual_total else None
    valid = len(paired) >= MIN_PAIRED_PARTICIPANTS and {p['manualFirst'] for p in paired} == {True, False}
    status = 'NOT MEASURED' if not valid else ('MET' if reduction is not None and reduction >= 70 else 'NOT MET')
    return {'version': STUDY_VERSION, 'sessions': len(sessions), 'participants': len(by_participant), 'paired': paired,
            'reduction': reduction, 'valid': valid, 'status': status,
            'rule': f'Reduction = (manual active time - AI-assisted active time) / manual active time, over participants who completed both '
                    f'modes. Reported as valid only with at least {MIN_PAIRED_PARTICIPANTS} paired participants covering both orders.'}


def study_results(request: Request):
    user = require_workspace_user(request, {'accountant', 'local_admin'})
    sessions = _sessions(user)
    return {**summarise(sessions), 'rows': sessions}


def study_results_csv(request: Request):
    user = require_workspace_user(request, {'accountant', 'local_admin'})
    columns = ['participant', 'mode', 'order', 'sampleFile', 'kind', 'activeSeconds', 'idleSeconds', 'pausedSeconds', 'fieldsTotal',
               'fieldsTyped', 'aiAccepted', 'aiCorrected', 'correct', 'quality', 'startedAt', 'completedAt', 'recordedAt', 'recordedBy']
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction='ignore')
    writer.writeheader()
    for row in _sessions(user):
        writer.writerow(row)
    return Response(buffer.getvalue(), media_type='text/csv',
                    headers={'Content-Disposition': 'attachment; filename="accordai-effort-study.csv"'})


def register(router, handlers):
    router.add_api_route('/api/effort-study/tasks', study_tasks, methods=['GET'])
    router.add_api_route('/api/effort-study/files/{sample_file}', study_file, methods=['GET'])
    router.add_api_route('/api/effort-study/sessions', record_session, methods=['POST'], status_code=201)
    router.add_api_route('/api/effort-study/results', study_results, methods=['GET'])
    router.add_api_route('/api/effort-study/results.csv', study_results_csv, methods=['GET'])
