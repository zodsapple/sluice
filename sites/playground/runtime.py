"""Browser adapter around the same Sluice pipeline and reviewed workflow."""
import json
import hashlib
import re
import shutil
import zipfile
from collections import Counter
from dataclasses import asdict
from decimal import Context, Decimal, InvalidOperation
from pathlib import Path

import yaml
import pandas as pd

from sluice.engine import run
from sluice.engine.locate import read_workbook
from sluice.engine import template as output_template
from sluice.spec import spec_from_dict
from sluice.spec.models import LocateSpec
from sluice import workflow
from sluice.agent.prompting import build_packet, parse_response, render_single_prompt
from sluice.infer.bindings import Binding, BindingError, validate

SESSION = Path('/session')


def reset_session():
    if SESSION.exists():
        shutil.rmtree(SESSION)
    SESSION.mkdir()
    from sluice.infer.blind import _PROFILES
    _PROFILES.clear()


def safe_spec(text):
    spec = spec_from_dict(yaml.safe_load(text))
    if spec.template.path or any(s.file for s in spec.sources):
        raise ValueError('Choose the template in the page. External file references are not supported here.')
    if any(op.op == 'hook' or 'python' in op.params() for op in [*spec.shape, *[o for s in spec.sources for o in s.shape]]):
        raise ValueError('Python hooks are disabled in the playground.')
    name = re.sub(r'\{[^{}]+\}', 'FIELD', spec.template.filename)
    if '/' in name or '\\' in name or name in ('.', '..'):
        raise ValueError('Use a plain output filename without folders.')
    if Path(spec.template.filename).suffix.lower() not in ('.csv', '.xlsx'):
        raise ValueError('Choose a .csv or .xlsx output filename.')
    return spec


def check_input(path):
    if path.suffix == '.xlsx':
        with zipfile.ZipFile(path) as archive:
            if sum(f.file_size for f in archive.infolist()) > 32 * 1024 * 1024:
                raise ValueError('This workbook expands beyond the 32 MB playground limit. Use the local CLI.')


def target_table(path):
    """A target is a flat table on the first sheet; never silently rename duplicate headers."""
    raw = next(iter(read_workbook(path, LocateSpec()).values()))
    if len(raw) > 20001:
        raise ValueError('The playground supports up to 20,000 target rows. Use the local CLI.')
    if raw.empty:
        raise ValueError('The target file is empty. Put column names in its first row.')
    headers = [str(v).strip() if pd.notna(v) else '' for v in raw.iloc[0]]
    if any(not h for h in headers) or len(set(headers)) != len(headers):
        raise ValueError('Target column names must be non-empty and unique in the first row.')
    frame = raw.iloc[1:].copy()
    frame.columns = headers
    return frame.reset_index(drop=True)


def field_type(series):
    values = [str(v).strip() for v in series if pd.notna(v) and str(v).strip()]
    if not values:
        return 'string'
    # Preserve identifiers such as 00123, and avoid guessing locale-specific numbers/dates.
    if all(re.fullmatch(r'[+-]?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?', v) for v in values):
        return 'decimal'
    if all(re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:[ T].*)?', v) for v in values):
        try:
            for v in values:
                pd.Timestamp(v)
            return 'date'
        except ValueError:
            pass
    return 'string'


def schema_from_target(frame, layout='auto'):
    names = {c.lower() for c in frame.columns}
    period = names & {'date', 'period', 'year', 'period_end', 'reporting_year'}
    mode = layout if layout != 'auto' else ('stats' if 'value' in names and period else 'record')
    return {'mode': mode, 'fields': [{'name': c, 'type': field_type(frame[c]), 'nullable': True} for c in frame.columns],
            'example_row': {c: ('' if pd.isna(v) else str(v)) for c, v in frame.iloc[0].items()} if len(frame) else {}}


def expected_control(expected, output):
    """Compare complete row multisets: ordering is irrelevant, duplicates and every value matter."""
    control = dict(name='expected_result', kind='expected_rows', source=len(expected), dropped=0,
                   output=len(output), expected=len(expected), diff=len(output) - len(expected),
                   passed=False, on_fail='halt', reasons=[])
    if set(expected.columns) != set(output.columns) or output.columns.duplicated().any():
        control['reasons'] = [f'Column names differ: expected {list(expected.columns)}, output {list(output.columns)}']
        return control
    kinds = [field_type(expected[c]) for c in expected.columns]
    def cell(value, kind):
        if pd.isna(value) or value == '':
            return ''
        if kind == 'decimal':
            number = Decimal(str(value).strip())
            if not number.is_finite():
                raise ValueError('Non-finite numeric value')
            return str(number.normalize(Context(prec=max(28, len(number.as_tuple().digits)))))
        if kind == 'date':
            return pd.Timestamp(value).isoformat()
        return str(value)
    def rows(frame):
        return Counter(tuple(cell(v, k) for v, k in zip(row, kinds))
                       for row in frame[list(expected.columns)].itertuples(index=False, name=None))
    try:
        want, got = rows(expected), rows(output)
    except (ValueError, InvalidOperation, OverflowError) as exc:
        control['reasons'] = [f'Expected-value comparison failed: {exc}']
        return control
    missing, extra = want - got, got - want
    control['dropped'] = sum(missing.values())
    control['passed'] = not missing and not extra
    if not control['passed']:
        control['reasons'] = [f'{sum(missing.values())} missing expected row(s); {sum(extra.values())} unexpected output row(s).']
        for label, items in [('Missing', missing), ('Unexpected', extra)]:
            if items:
                row, count = next(iter(items.items()))
                control['reasons'].append(f'{label} example ({count}×): {dict(zip(expected.columns, row))}')
    return control


def file_identity(path):
    return {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'extension': path.suffix.lower()}


def build_workspace(config, path):
    """Rebuild from portable decisions, rather than a worker's temporary filesystem state."""
    schema = SESSION / 'schema.json'
    schema.write_text(json.dumps(config['target_schema']), encoding='utf-8')
    bindings = {}
    for answer in config.get('answers', {}).values():
        if not isinstance(answer, dict) or answer.get('status') not in ('answered', 'unknown') or not isinstance(answer.get('bindings'), list) or not isinstance(answer.get('sheet'), str):
            raise ValueError('Saved answers need status, sheet and a bindings array.')
        if answer['status'] == 'answered':
            if not answer['bindings'] or any(not isinstance(b, dict) or b.get('kind') == 'unknown' for b in answer['bindings']):
                raise ValueError('An answered decision needs concrete bindings; unknown decisions must remain open.')
            bindings.setdefault(answer['sheet'], []).extend(answer['bindings'])
    draft = workflow.prepare(path, schema, SESSION / 'work', sheets=config['sheets'], bindings=bindings)
    if draft.rejected:
        invalid = [(s, b, why) for s, b, why in draft.rejected if b.origin in ('human', 'llm')]
        if invalid:
            raise ValueError('Invalid saved decision: ' + '; '.join(f'{s}:{b.target or b.kind}: {why}' for s, b, why in invalid))
    return draft, workflow.load_target(schema)


def workspace_result(config, draft, target, notices=None):
    prompts, contexts = [], {}
    for frame, bindings in zip(draft.frames, draft.bindings):
        questions = [q for q in draft.questions if q.frame == frame.sheet]
        context = frame.context(target.fields)
        contexts[frame.sheet] = {'sources': sorted(context.sources), 'dimensions': context.dim_values, 'cells': frame.facts,
                                'labels': {n: s.origin for n, s in frame.sources.items()}}
        if questions:
            asked = {t for q in questions for t in q.targets}
            packet = build_packet(frame, questions, target.fields, {b.target: f'{b.kind} {b.args}' for b in bindings if b.target and b.target not in asked},
                                  conventions=target.notes, example=target.example)
            samples = {t: config.get('target_samples', {})[t] for t in asked if t in config.get('target_samples', {})}
            if samples:
                packet['target_value_samples'] = samples
            reply = {'answers': [{'question_id': q.id, 'status': 'unknown', 'bindings': []} for q in questions]}
            prompts.append({'sheet': frame.sheet, 'text': render_single_prompt(packet), 'reply': reply})
    review = (SESSION / 'work/review.md').read_text()
    review += '\nDraft source sheets: ' + ', '.join(config['sheets']) + '. Review the extraction scope.\n'
    if config['target_role'] == 'expected':
        review = review.replace('No reconciliation controls declared. Add controls to spec.yaml before approval.',
                                'The complete expected file supplies a halting row comparison.')
    return {'config': config, 'spec': yaml.safe_dump(config['spec'], sort_keys=False, allow_unicode=True),
            'questions': [asdict(q) for q in draft.questions], 'review': review, 'contexts': contexts,
            'promptPackets': prompts, 'prompts': [p['text'] for p in prompts], 'notices': notices or []}


def check_config(config, path, target_path, role):
    if not isinstance(config, dict) or config.get('format') != 'sluice-playground' or config.get('version') != 1:
        raise ValueError('Choose a Sluice playground config, version 1.')
    identity = {'input': file_identity(path), 'target': file_identity(target_path) if target_path else None}
    if config.get('files') != identity:
        raise ValueError('This config belongs to a different input / target file pair. Load its original files or generate a new config.')
    if config.get('target_role') != role:
        raise ValueError('Config target role differs. Select its Expected result or Template / example mode first.')
    if not isinstance(config.get('answers', {}), dict) or not isinstance(config.get('sheets'), list) or not config['sheets']:
        raise ValueError('Config needs source sheets and an answers object.')
    safe_spec(json.dumps(config['spec']))


def apply_reply(config, text, base, target, *, manual=False):
    answers = parse_response(text)
    if not answers:
        raise ValueError('The reply contains no answers.')
    known = {q.id: q for q in base.questions}
    frames = {f.sheet: f for f in base.frames}
    proposed = {}
    for answer in answers:
        if isinstance(answer, dict) and answer.get('question_id') in known and answer.get('status') != 'unknown':
            q = known[answer['question_id']]
            for b in answer.get('bindings', []) if isinstance(answer.get('bindings'), list) else []:
                if isinstance(b, dict) and b.get('target') in q.targets and b.get('kind') != 'unknown':
                    proposed.setdefault(q.frame, set()).add(b['target'])
    notices, seen = [], set()
    for answer in answers:
        if not isinstance(answer, dict) or answer.get('question_id') not in known:
            raise ValueError('Reply contains an unknown question_id. Use the prompt for this file pair.')
        q = known[answer['question_id']]
        if q.id in seen:
            raise ValueError(f'Duplicate answer for {q.id}.')
        seen.add(q.id)
        status = answer.get('status', 'answered' if answer.get('bindings') else 'unknown')
        raw = answer.get('bindings', [])
        if status not in ('answered', 'unknown') or not isinstance(raw, list):
            raise ValueError(f'{q.id}: use status answered or unknown, and a bindings array.')
        if status == 'unknown':
            notices.append(f'{q.id}: still unknown; enter a manual decision.')
            continue
        if not raw:
            raise ValueError(f'{q.id}: an answered question needs bindings.')
        sheet_bindings = dict(zip((f.sheet for f in base.frames), base.bindings))[q.frame]
        saved = {b.get('target') for a in config.get('answers', {}).values() if a['sheet'] == q.frame
                 for b in a['bindings'] if b.get('target')}
        context = frames[q.frame].context(target.fields, {b.target for b in sheet_bindings if b.target} | saved | proposed.get(q.frame, set()))
        accepted = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError(f'{q.id}: each binding must be an object.')
            b = Binding.from_dict({**item, 'origin': 'human' if manual else 'llm', **({'confidence': 1} if manual else {})})
            if b.kind == 'unknown':
                notices.append(f'{q.id}: still unknown; enter a manual decision.')
                accepted = []
                break
            if q.kind == 'scope' and b.kind not in ('grid_scope', 'row_scope'):
                raise ValueError(f'{q.id}: answer with a row_scope or grid_scope binding.')
            if q.kind != 'scope' and b.target not in q.targets:
                raise ValueError(f'{q.id}: binding target must be one of {q.targets}.')
            validate(b, context)
            if not manual and b.confidence < .7:
                notices.append(f'{q.id}: confidence {b.confidence} needs manual confirmation.')
                accepted = []
                break
            context.bound.add(b.target) if b.target else None
            accepted.append(b.to_dict())
        if accepted:
            config.setdefault('answers', {})[q.id] = {'sheet': q.frame, 'status': 'answered', 'bindings': accepted}
    return notices


def execute(request_json):
    req = json.loads(request_json)
    path = SESSION / req['input']
    check_input(path)
    template = SESSION / req['template'] if req.get('template') else None
    if template:
        check_input(template)
    expected = target_table(template) if template and req.get('targetRole') == 'expected' else None
    if expected is not None and expected.empty:
        raise ValueError('An expected result needs data rows. For headers only, choose Template / example.')
    config = req.get('config')
    if config is not None:
        check_config(config, path, template, req.get('targetRole', 'template'))
    if req['action'] == 'draft':
        raw = read_workbook(path, LocateSpec())
        if sum(len(df) for df in raw.values()) > 20000:
            raise ValueError('The playground supports up to 20,000 source rows. Use the local CLI for larger files.')
        schema = SESSION / 'schema.json'
        if not req.get('schema') and not template:
            raise ValueError('Choose an expected result, a template or a JSON target schema to draft rules.')
        schema.write_text(req.get('schema') or json.dumps(schema_from_target(
            expected if expected is not None else target_table(template), req.get('targetLayout', 'auto'))), encoding='utf-8')
        sheets = req.get('sheets') or None
        if sheets is None and expected is not None and 'source_sheet' in expected:
            declared = expected['source_sheet'].dropna().astype(str).unique().tolist()
            if declared and all(s in raw for s in declared):
                sheets = declared
        draft = workflow.prepare(path, schema, SESSION / 'work', sheets=sheets)
        config = {'format': 'sluice-playground', 'version': 1,
                  'files': {'input': file_identity(path), 'target': file_identity(template) if template else None},
                  'target_role': req.get('targetRole', 'template'), 'target_schema': json.loads(schema.read_text()),
                  'sheets': sheets or list(raw), 'answers': {}, 'spec': draft.spec}
        if template:
            table = expected if expected is not None else target_table(template)
            config['target_samples'] = {}
            for col in table:
                values = table[col].dropna().astype(str).unique().tolist()
                config['target_samples'][col] = {'values': [v[:120] for v in values[:8]], 'count': len(values), 'partial': len(values) > 8}
        result = workspace_result(config, draft, workflow.load_target(schema))
        if not req.get('schema'):
            result['review'] += '\nField types and example values were inferred from the target file. Check every mapping, constant and row scope.\n'
        if expected is not None:
            result['review'] += '\nThe complete expected result will be checked before output is written. Row order is ignored; duplicate rows and all values must match.\n'
        elif not draft.spec.get('controls'):
            result['review'] += '\nA template describes the format only. Add halting reconciliation controls, or supply the complete expected result before converting.\n'
        return json.dumps(result, default=str)
    questions = []
    if config is not None:
        if req['action'] == 'answer':
            base_config = {**config, 'answers': {}}
            base, target = build_workspace(base_config, path)
            notices = apply_reply(config, req.get('reply', ''), base, target, manual=req.get('manual') is True)
            draft, target = build_workspace(config, path)
            # Decisions rebuild mapping/shape; keep independently edited controls and output settings.
            for key in ('controls', 'keys', 'rules', 'description'):
                if key in config['spec']:
                    draft.spec[key] = config['spec'][key]
            draft.spec['template'].update({k: v for k, v in config['spec']['template'].items() if k != 'columns'})
            config['spec'] = draft.spec
            safe_spec(json.dumps(config['spec']))
            return json.dumps(workspace_result(config, draft, target, notices), default=str)
        draft, target = build_workspace(config, path)
        questions = draft.questions
        if req['action'] == 'edit_yaml':
            safe_spec(req['spec'])
            config['spec'] = yaml.safe_load(req['spec'])
        if req['action'] in ('load_config', 'edit_yaml'):
            return json.dumps(workspace_result(config, draft, target), default=str)
        if req['action'] == 'convert' and questions:
            raise ValueError(f'Resolve the {len(questions)} open question(s) before converting.')
    spec = safe_spec(json.dumps(config['spec']) if config is not None else req['spec'])
    if req['action'] == 'convert' and expected is None and not any(c.on_fail == 'halt' for c in spec.controls):
        raise ValueError('Add a halting reconciliation control before converting.')
    source = read_workbook(path, spec.locate)
    if sum(len(df) for df in source.values()) > 20000:
        raise ValueError('The playground supports up to 20,000 source rows. Use the local CLI for larger files.')
    preview = {name: {'rows': len(df), 'cells': df.head(8).fillna('').astype(str).values.tolist()}
               for name, df in source.items()}
    # Dry-run first when checking expected data, so a mismatch can never create a downloadable output.
    result = run(spec, path, SESSION / 'out', template if expected is None else None,
                 write_output=req['action'] == 'convert' and expected is None and config is None, write_logs=True)
    report = json.loads(Path(result.log_path).read_text())
    feedback = Path(result.feedback_path).read_text() if result.feedback_path else ''
    if config is not None:
        report['controls'].append(dict(name='configuration_questions', kind='config_questions', source=len(questions),
                                       dropped=0, output=0, expected=0, diff=len(questions), passed=not questions,
                                       on_fail='halt', reasons=[f'{len(questions)} unresolved decision(s).'] if questions else []))
        if questions:
            result.status = report['status'] = 'failed'
            report['diagnosis'] = dict(stage='output_validation', code='unresolved_config',
                                       message=f'Resolve {len(questions)} open question(s); use a chatbot reply or manual entry.')
    if expected is not None and result.output_frame is not None:
        control = expected_control(expected, result.output_frame)
        report['controls'].append(control)
        if not control['passed']:
            result.status = report['status'] = 'failed'
            report['diagnosis'] = dict(stage='output_validation', code='expected_mismatch', message='; '.join(control['reasons']))
            feedback += '\n## Expected result mismatch\n\n' + '\n'.join(control['reasons']) + '\n'
    if result.ok and req['action'] == 'convert' and (expected is not None or config is not None):
        dest = SESSION / 'out' / output_template.output_name(spec.template, result.output_frame, path.stem, spec.name)
        output_template.write_output(result.output_frame, spec.template, template if expected is None else None, dest)
        result.output_path = report['output_path'] = str(dest)
    Path(result.log_path).write_text(json.dumps(report, default=str), encoding='utf-8')
    table = {'columns': list(result.output_frame.columns),
             'rows': result.output_frame.head(12).fillna('').astype(str).values.tolist()} if result.output_frame is not None else None
    return json.dumps({'ok': result.ok, 'report': report, 'inputPreview': preview, 'outputPreview': table,
                       'output': result.output_path,
                       'feedback': feedback}, default=str)
