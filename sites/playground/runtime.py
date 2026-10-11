"""Browser adapter around the same Sluice pipeline and reviewed workflow."""
import json
import re
import shutil
import zipfile
from dataclasses import asdict
from pathlib import Path

import yaml

from sluice.engine import run
from sluice.engine.locate import read_workbook
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


def execute(request_json):
    req = json.loads(request_json)
    path = SESSION / req['input']
    check_input(path)
    template = SESSION / req['template'] if req.get('template') else None
    if template:
        check_input(template)
    if req['action'] == 'draft':
        if sum(len(df) for df in read_workbook(path, LocateSpec()).values()) > 20000:
            raise ValueError('The playground supports up to 20,000 source rows. Use the local CLI for larger files.')
        schema = SESSION / 'schema.json'
        schema.write_text(req['schema'], encoding='utf-8')
        draft = workflow.prepare(path, schema, SESSION / 'work', sheets=req.get('sheets') or None)
        return json.dumps({'spec': (SESSION / 'work/spec.yaml').read_text(),
                           'questions': [asdict(q) for q in draft.questions],
                           'review': (SESSION / 'work/review.md').read_text(),
                           'prompts': [p.read_text() for p in sorted((SESSION / 'work/prompts').glob('*.txt'))]}, default=str)
    spec = safe_spec(req['spec'])
    if req['action'] == 'convert' and not any(c.on_fail == 'halt' for c in spec.controls):
        raise ValueError('Add a halting reconciliation control before converting.')
    source = read_workbook(path, spec.locate)
    if sum(len(df) for df in source.values()) > 20000:
        raise ValueError('The playground supports up to 20,000 source rows. Use the local CLI for larger files.')
    preview = {name: {'rows': len(df), 'cells': df.head(8).fillna('').astype(str).values.tolist()}
               for name, df in source.items()}
    result = run(spec, path, SESSION / 'out', template, write_output=req['action'] == 'convert', write_logs=True)
    report = json.loads(Path(result.log_path).read_text())
    table = {'columns': list(result.output_frame.columns),
             'rows': result.output_frame.head(12).fillna('').astype(str).values.tolist()} if result.output_frame is not None else None
    return json.dumps({'ok': result.ok, 'report': report, 'inputPreview': preview, 'outputPreview': table,
                       'output': result.output_path,
                       'feedback': Path(result.feedback_path).read_text() if result.feedback_path else ''}, default=str)
