"""Browser adapter around the same Sluice pipeline and reviewed workflow."""
import json
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
        review = (SESSION / 'work/review.md').read_text()
        review += '\nDraft source sheets: ' + ', '.join(sheets or raw) + '. Review the extraction scope.\n'
        if not req.get('schema'):
            review += '\nField types and example values were inferred from the target file. Check every mapping, constant and row scope.\n'
        if expected is not None:
            review = review.replace('No reconciliation controls declared. Add controls to spec.yaml before approval.',
                                    'No reconciliation controls declared in YAML; the complete expected file supplies a halting comparison.')
            review += '\nThe complete expected result will be checked before output is written. Row order is ignored; duplicate rows and all values must match.\n'
        elif not draft.spec.get('controls'):
            review += '\nA template describes the format only. Add halting reconciliation controls, or supply the complete expected result before converting.\n'
        return json.dumps({'spec': (SESSION / 'work/spec.yaml').read_text(),
                           'questions': [asdict(q) for q in draft.questions],
                           'review': review,
                           'prompts': [p.read_text() for p in sorted((SESSION / 'work/prompts').glob('*.txt'))]}, default=str)
    spec = safe_spec(req['spec'])
    if req['action'] == 'convert' and expected is None and not any(c.on_fail == 'halt' for c in spec.controls):
        raise ValueError('Add a halting reconciliation control before converting.')
    source = read_workbook(path, spec.locate)
    if sum(len(df) for df in source.values()) > 20000:
        raise ValueError('The playground supports up to 20,000 source rows. Use the local CLI for larger files.')
    preview = {name: {'rows': len(df), 'cells': df.head(8).fillna('').astype(str).values.tolist()}
               for name, df in source.items()}
    # Dry-run first when checking expected data, so a mismatch can never create a downloadable output.
    result = run(spec, path, SESSION / 'out', template if expected is None else None,
                 write_output=req['action'] == 'convert' and expected is None, write_logs=True)
    report = json.loads(Path(result.log_path).read_text())
    feedback = Path(result.feedback_path).read_text() if result.feedback_path else ''
    if expected is not None and result.output_frame is not None:
        control = expected_control(expected, result.output_frame)
        report['controls'].append(control)
        if not control['passed']:
            result.status = report['status'] = 'failed'
            report['diagnosis'] = dict(stage='output_validation', code='expected_mismatch', message='; '.join(control['reasons']))
            feedback += '\n## Expected result mismatch\n\n' + '\n'.join(control['reasons']) + '\n'
        if result.ok and req['action'] == 'convert':
            dest = SESSION / 'out' / output_template.output_name(spec.template, result.output_frame, path.stem, spec.name)
            output_template.write_output(result.output_frame, spec.template, None, dest)
            result.output_path = report['output_path'] = str(dest)
        Path(result.log_path).write_text(json.dumps(report, default=str), encoding='utf-8')
    table = {'columns': list(result.output_frame.columns),
             'rows': result.output_frame.head(12).fillna('').astype(str).values.tolist()} if result.output_frame is not None else None
    return json.dumps({'ok': result.ok, 'report': report, 'inputPreview': preview, 'outputPreview': table,
                       'output': result.output_path,
                       'feedback': feedback}, default=str)
