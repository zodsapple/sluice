"""The browser bridge uses the same engine, writes only validated output, and refuses unsupported specs."""
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def browser(tmp_path):
    spec = importlib.util.spec_from_file_location("browser_adapter", ROOT / "sites/playground/runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.SESSION = tmp_path
    module.reset_session()
    for name in ("input.csv", "template.csv"):
        shutil.copyfile(ROOT / "sluice/examples/quickstart" / name, tmp_path / name)
    return module


def request(**overrides):
    return {"action": "preview", "input": "input.csv", "template": "template.csv",
            "spec": (ROOT / "sluice/examples/quickstart/spec.yaml").read_text(), **overrides}


def test_browser_preview_convert_and_failure(browser):
    preview = json.loads(browser.execute(json.dumps(request())))
    assert preview["ok"] and preview["output"] is None and len(preview["outputPreview"]["rows"]) == 3
    converted = json.loads(browser.execute(json.dumps(request(action="convert"))))
    assert converted["ok"] and Path(converted["output"]).exists()
    source = browser.SESSION / "input.csv"
    source.write_text(source.read_text().replace("100.125", "100.129"))
    failed = json.loads(browser.execute(json.dumps(request())))
    assert not failed["ok"] and failed["output"] is None


def test_browser_transport_template_and_generated_filename(browser):
    fix = ROOT / "sluice/tests/fixtures/transport"
    shutil.copyfile(fix / "input/DHL_DE_202604_Transport.xlsx", browser.SESSION / "input.xlsx")
    shutil.copyfile(fix / "transport_template.xlsx", browser.SESSION / "template.xlsx")
    spec = (ROOT / "sluice/specs/dhl_de_transport.yaml").read_text()
    result = json.loads(browser.execute(json.dumps(request(action="convert", input="input.xlsx", template="template.xlsx", spec=spec))))
    assert result["ok"] and result["output"].endswith("CTD_EMEA_TRANSPORT_DE_DHL_202604.xlsx")


def test_browser_schema_drafting(browser):
    schema = (ROOT / "sluice/examples/quickstart/schema.json").read_text()
    draft = json.loads(browser.execute(json.dumps(request(action="draft", schema=schema))))
    assert "declared_total" in draft["spec"] and not draft["questions"]


@pytest.mark.parametrize("action", ["preview", "draft"])
def test_browser_large_input_uses_local_cli(browser, action):
    (browser.SESSION / "input.csv").write_text("delivery_id\n" + "A001\n" * 20001)
    schema = (ROOT / "sluice/examples/quickstart/schema.json").read_text()
    with pytest.raises(ValueError, match="20,000"):
        browser.execute(json.dumps(request(action=action, schema=schema)))


@pytest.mark.parametrize("fragment", ["shape: [{op: hook, python: 'os:system'}]", "sources: [{name: other, file: '/private.xlsx', map: {}}]"])
def test_browser_disables_hooks_and_external_sources(browser, fragment):
    with pytest.raises(ValueError):
        browser.safe_spec(request()["spec"] + "\n" + fragment)


@pytest.mark.parametrize('extension', ['csv', 'xlsx'])
def test_browser_drafts_from_target_without_json(browser, extension):
    import pandas as pd
    target = pd.DataFrame({'delivery_id': ['A001', 'A002', 'A003'], 'amount': [100.125, 50.25, 15.125]})
    path = browser.SESSION / f'expected.{extension}'
    if extension == 'xlsx':
        target.to_excel(path, index=False)
    else:
        target.to_csv(path, index=False)
    draft = json.loads(browser.execute(json.dumps(request(action='draft', schema='', template=path.name,
                                                          targetRole='expected'))))
    assert 'delivery_id:' in draft['spec'] and 'amount:' in draft['spec']
    assert 'complete expected result' in draft['review']
    preview = json.loads(browser.execute(json.dumps(request(spec=draft['spec'], template=path.name,
                                                            targetRole='expected'))))
    assert preview['ok'], preview['report']['diagnosis']
    assert preview['report']['controls'][-1]['kind'] == 'expected_rows'


def test_expected_checks_values_duplicates_and_never_writes_failed_output(browser):
    import pandas as pd
    reference = json.loads(browser.execute(json.dumps(request())))['outputPreview']
    frame = pd.DataFrame(reference['rows'], columns=reference['columns'])
    frame.iloc[::-1].to_csv(browser.SESSION / 'expected.csv', index=False)
    req = request(action='convert', template='expected.csv', targetRole='expected')
    good = json.loads(browser.execute(json.dumps(req)))
    assert good['ok'] and Path(good['output']).exists()
    # A different ID with identical row counts and money totals must still fail.
    frame.iloc[0, 0] = 'WRONG'
    frame.to_csv(browser.SESSION / 'expected.csv', index=False)
    existing = {p: p.read_bytes() for p in (browser.SESSION / 'out').glob('*.csv')}
    bad = json.loads(browser.execute(json.dumps(req)))
    assert not bad['ok'] and bad['output'] is None
    assert bad['report']['diagnosis']['code'] == 'expected_mismatch'
    assert {p: p.read_bytes() for p in (browser.SESSION / 'out').glob('*.csv')} == existing
    duplicate = pd.DataFrame({'id': ['001', '001'], 'amount': ['1.00', '2.00']})
    assert not browser.expected_control(duplicate, duplicate.iloc[[0, 0]])['passed']
    assert browser.expected_control(duplicate, duplicate.iloc[::-1])['passed']
    assert not browser.expected_control(duplicate, duplicate.assign(id=['1', '1']))['passed']


def test_target_headers_and_header_only_template(browser):
    (browser.SESSION / 'target.csv').write_text('id,id\n1,2\n')
    with pytest.raises(ValueError, match='unique'):
        browser.execute(json.dumps(request(action='draft', template='target.csv', schema='')))
    (browser.SESSION / 'target.csv').write_text('delivery_id,amount\n')
    with pytest.raises(ValueError, match='needs data rows'):
        browser.execute(json.dumps(request(action='draft', template='target.csv', schema='', targetRole='expected')))
    draft = json.loads(browser.execute(json.dumps(request(action='draft', template='target.csv', schema='', targetRole='template'))))
    assert 'delivery_id:' in draft['spec']


def test_expected_sheet_provenance_limits_draft_scope(browser):
    import pandas as pd
    import yaml
    with pd.ExcelWriter(browser.SESSION / 'input.xlsx') as writer:
        pd.DataFrame({'id': ['001'], 'amount': [1]}).to_excel(writer, sheet_name='Data', index=False)
        pd.DataFrame({'notes': ['Do not extract me']}).to_excel(writer, sheet_name='Notes', index=False)
    (browser.SESSION / 'expected.csv').write_text('id,amount,source_sheet\n001,1,Data\n')
    draft = json.loads(browser.execute(json.dumps(request(action='draft', input='input.xlsx', template='expected.csv',
                                                          targetRole='expected', schema=''))))
    spec = yaml.safe_load(draft['spec'])
    assert spec['locate']['sheet']['name'] == 'Data' and not spec['sources']
    assert 'Draft source sheets: Data' in draft['review']


def decision_draft(browser, extra_fields=('currency',)):
    import pandas as pd
    schema = json.loads((ROOT / 'sluice/examples/quickstart/schema.json').read_text())
    frame = pd.read_csv(browser.SESSION / 'input.csv', dtype=str)
    for name in extra_fields:
        schema['fields'].append({'name': name, 'type': 'string', 'nullable': False})
        schema['example_row'][name] = 'EUR' if name == 'currency' else 'confirmed'
        frame[name] = schema['example_row'][name]
    frame.to_csv(browser.SESSION / 'expected.csv', index=False)
    req = request(action='draft', template='expected.csv', targetRole='expected', schema=json.dumps(schema))
    return req, json.loads(browser.execute(json.dumps(req)))


def reply_for(question, value, confidence=1):
    return {'question_id': question['id'], 'status': 'answered', 'bindings': [
        {'kind': 'constant', 'target': question['targets'][0], 'args': {'value': value}, 'confidence': confidence}]}


def test_json_config_prompt_fenced_reply_and_validated_conversion(browser):
    req, draft = decision_draft(browser)
    assert draft['config']['files']['input']['sha256'] and len(draft['questions']) == 1
    prompt = draft['promptPackets'][0]
    assert '"answers"' in prompt['text'] and 'example_row' in prompt['text'] and 'EUR' in prompt['text']
    assert prompt['reply']['answers'][0]['status'] == 'unknown'
    reply = '```json\n' + json.dumps({'answers': [reply_for(draft['questions'][0], 'EUR')]}) + '\n```'
    resolved = json.loads(browser.execute(json.dumps({**req, 'action': 'answer', 'config': draft['config'], 'reply': reply})))
    assert not resolved['questions'] and not resolved['prompts']
    assert resolved['config']['spec']['template']['columns']['currency']['col'] == 'currency'
    result = json.loads(browser.execute(json.dumps({**req, 'action': 'convert', 'config': resolved['config']})))
    assert result['ok'] and Path(result['output']).exists()
    controls = {c['name']: c['passed'] for c in result['report']['controls']}
    assert controls['configuration_questions'] and controls['expected_result']


def test_partial_reply_manual_entry_and_config_roundtrip(browser):
    req, draft = decision_draft(browser, ('currency', 'review_status'))
    q = {x['targets'][0]: x for x in draft['questions']}
    partial = json.loads(browser.execute(json.dumps({**req, 'action': 'answer', 'config': draft['config'],
                         'reply': json.dumps({'answers': [reply_for(q['currency'], 'EUR')]})})))
    assert len(partial['questions']) == 1 and partial['questions'][0]['targets'] == ['review_status']
    complete = json.loads(browser.execute(json.dumps({**req, 'action': 'answer', 'config': partial['config'], 'manual': True,
                          'reply': json.dumps({'answers': [reply_for(q['review_status'], 'confirmed', .1)]})})))
    assert not complete['questions']
    assert complete['config']['answers'][q['review_status']['id']]['bindings'][0]['origin'] == 'human'
    # Round-trip after a worker filesystem reset; no hidden worker state is needed.
    original = {f.name: f.read_bytes() for f in browser.SESSION.glob('*.csv')}
    browser.reset_session()
    for name, data in original.items():
        (browser.SESSION / name).write_bytes(data)
    restored = json.loads(browser.execute(json.dumps({**req, 'action': 'load_config', 'config': complete['config']})))
    assert restored['config'] == complete['config'] and not restored['questions']


@pytest.mark.parametrize('mode', ['unknown', 'low_confidence'])
def test_unknown_or_low_confidence_needs_manual_decision(browser, mode):
    req, draft = decision_draft(browser)
    q = draft['questions'][0]
    answer = {'question_id': q['id'], 'status': 'unknown', 'bindings': []} if mode == 'unknown' else reply_for(q, 'EUR', .2)
    result = json.loads(browser.execute(json.dumps({**req, 'action': 'answer', 'config': draft['config'],
                       'reply': json.dumps({'answers': [answer]})})))
    assert len(result['questions']) == 1 and result['notices'] and not result['config']['answers']
    with pytest.raises(ValueError, match='open question'):
        browser.execute(json.dumps({**req, 'action': 'convert', 'config': result['config']}))
    assert not list((browser.SESSION / 'out').glob('*.csv'))


@pytest.mark.parametrize('mode', ['bad_json', 'foreign_question', 'wrong_target', 'unknown_source', 'duplicate'])
def test_bad_reply_is_rejected_atomically(browser, mode):
    req, draft = decision_draft(browser)
    original = json.dumps(draft['config'], sort_keys=True)
    answer = reply_for(draft['questions'][0], 'EUR')
    if mode == 'foreign_question':
        answer['question_id'] = 'another-file:currency'
    if mode == 'wrong_target':
        answer['bindings'][0]['target'] = 'amount'
    if mode == 'unknown_source':
        answer['bindings'][0] = {'kind': 'column', 'target': 'currency', 'args': {'source': 'nonexistent'}}
    reply = 'not JSON' if mode == 'bad_json' else json.dumps({'answers': [answer, answer] if mode == 'duplicate' else [answer]})
    with pytest.raises(ValueError, match='JSON|question|target|source|Duplicate'):
        browser.execute(json.dumps({**req, 'action': 'answer', 'config': draft['config'], 'reply': reply}))
    assert json.dumps(draft['config'], sort_keys=True) == original


def test_config_hash_and_expected_gate_survive_valid_answers(browser):
    req, draft = decision_draft(browser)
    wrong = json.loads(browser.execute(json.dumps({**req, 'action': 'answer', 'config': draft['config'], 'manual': True,
                      'reply': json.dumps({'answers': [reply_for(draft['questions'][0], 'USD')]})})))
    failed = json.loads(browser.execute(json.dumps({**req, 'action': 'convert', 'config': wrong['config']})))
    assert not failed['ok'] and failed['output'] is None and failed['report']['diagnosis']['code'] == 'expected_mismatch'
    path = browser.SESSION / 'input.csv'
    path.write_text(path.read_text().replace('A001', 'A999'))
    with pytest.raises(ValueError, match='different input'):
        browser.execute(json.dumps({**req, 'action': 'load_config', 'config': wrong['config']}))


def test_reply_field_dependencies_work_in_reverse_order(browser):
    import pandas as pd
    req, _ = decision_draft(browser, ('currency', 'review_status'))
    schema = json.loads(req['schema'])
    schema['example_row']['review_status'] = 'EUR'
    req['schema'] = json.dumps(schema)
    path = browser.SESSION / 'expected.csv'
    frame = pd.read_csv(path, dtype=str)
    frame['review_status'] = 'EUR'
    frame.to_csv(path, index=False)
    draft = json.loads(browser.execute(json.dumps(req)))
    questions = {q['targets'][0]: q for q in draft['questions']}
    dependent = {'question_id': questions['review_status']['id'], 'status': 'answered', 'bindings': [
        {'kind': 'template', 'target': 'review_status', 'args': {'template': '{currency}'}, 'confidence': .95}]}
    reply = {'answers': [dependent, reply_for(questions['currency'], 'EUR')]}
    resolved = json.loads(browser.execute(json.dumps({**req, 'action': 'answer', 'config': draft['config'], 'reply': json.dumps(reply)})))
    assert not resolved['questions']
    converted = json.loads(browser.execute(json.dumps({**req, 'action': 'convert', 'config': resolved['config']})))
    assert converted['ok'] and all(row[-2:] == ['EUR', 'EUR'] for row in converted['outputPreview']['rows'])


def test_imported_unknown_binding_cannot_silently_close_a_question(browser):
    req, draft = decision_draft(browser)
    q = draft['questions'][0]
    draft['config']['answers'][q['id']] = {'sheet': q['frame'], 'status': 'answered', 'bindings': [
        {'kind': 'unknown', 'target': 'currency', 'args': {'reason': 'not decided'}, 'origin': 'human'}]}
    with pytest.raises(ValueError, match='unknown decisions must remain open'):
        browser.execute(json.dumps({**req, 'action': 'load_config', 'config': draft['config']}))


@pytest.fixture
def scope_context():
    from types import SimpleNamespace
    from sluice.infer.bindings import TargetField, ValidationContext
    fields = {'value': TargetField('value', 'decimal')}
    def context(*_):
        return ValidationContext(fields, {'year', 'dim1'}, dim_values={'dim1': ['North', 'South']})
    frame = SimpleNamespace(sheet='s1', layout='grid', context=context)
    questions = [SimpleNamespace(id='s1:' + kind, frame='s1', targets=[], kind='scope')
                 for kind in ('grid_scope', 'row_scope')]
    return SimpleNamespace(frames=[frame], bindings=[[]], questions=questions), SimpleNamespace(fields=fields)


@pytest.mark.parametrize('extra_target', ['', 'dim1', 'value', 's1:grid_scope', None])
def test_scope_reply_normalizes_redundant_target_and_keeps_scope(browser, scope_context, extra_target):
    base, target = scope_context
    config = {'answers': {}}
    bindings = [{'kind': 'grid_scope', 'target': extra_target, 'args': {'dimension': 'dim1', 'include': ['North']}},
                {'kind': 'row_scope', 'target': extra_target, 'args': {'conditions': [{'column': 'year', 'equals': '2025'}]}}]
    answers = [{'question_id': q.id, 'status': 'answered', 'bindings': [b]} for q, b in zip(base.questions, bindings)]
    notices = browser.apply_reply(config, json.dumps({'answers': answers}), base, target)
    for q, binding in zip(base.questions, bindings):
        saved = config['answers'][q.id]['bindings'][0]
        assert saved['target'] is None and saved['args'] == binding['args']
    assert len(notices) == (0 if extra_target is None else 2)


@pytest.mark.parametrize('args', [{'dimension': 'dim9', 'include': ['North']},
                                  {'dimension': 'dim1', 'include': ['Invented']}])
def test_scope_target_normalization_does_not_accept_invented_scope(browser, scope_context, args):
    base, target = scope_context
    config = {'answers': {}}
    reply = {'answers': [{'question_id': 's1:grid_scope', 'status': 'answered', 'bindings': [
        {'kind': 'grid_scope', 'target': 'value', 'args': args}]}]}
    with pytest.raises(ValueError, match='s1:grid_scope:'):
        browser.apply_reply(config, json.dumps(reply), base, target)
    assert config['answers'] == {}


def test_browser_user_error_has_question_id_without_traceback(browser):
    req, draft = decision_draft(browser)
    answer = reply_for(draft['questions'][0], 'EUR')
    answer['bindings'][0] = {'kind': 'column', 'target': 'currency', 'args': {'source': 'invented'}}
    request_json = json.dumps({**req, 'action': 'answer', 'config': draft['config'], 'reply': json.dumps({'answers': [answer]})})
    result = json.loads(browser.execute_browser(request_json))
    assert 'input:currency:' in result['error'] and 'unknown source' in result['error']
    assert 'Traceback' not in result['error'] and draft['config']['answers'] == {}
    # Success follows the same worker entry point.
    good = reply_for(draft['questions'][0], 'EUR')
    result = json.loads(browser.execute_browser(json.dumps({**req, 'action': 'answer', 'config': draft['config'],
                       'reply': json.dumps({'answers': [good]})})))
    assert 'error' not in result and not result['questions']


def test_completed_decision_can_be_corrected_after_failed_validation(browser):
    req, draft = decision_draft(browser)
    q = draft['questions'][0]
    def answer(config, value):
        return json.loads(browser.execute(json.dumps({**req, 'action': 'answer', 'config': config,
                          'reply': json.dumps({'answers': [reply_for(q, value)]})})))
    wrong = answer(draft['config'], 'USD')
    assert not wrong['questions']
    stopped = json.loads(browser.execute(json.dumps({**req, 'action': 'convert', 'config': wrong['config']})))
    assert not stopped['ok'] and stopped['output'] is None
    corrected = answer(wrong['config'], 'EUR')
    assert not corrected['questions'] and len(corrected['config']['answers']) == 1
    passed = json.loads(browser.execute(json.dumps({**req, 'action': 'convert', 'config': corrected['config']})))
    assert passed['ok'] and Path(passed['output']).exists()


def test_validation_repair_prompt_current_decision_partial_reply_and_success(browser):
    req, draft = decision_draft(browser)
    q = draft['questions'][0]
    def call(action, config, **extra):
        return json.loads(browser.execute(json.dumps({**req, 'action': action, 'config': config, **extra})))
    wrong = call('answer', draft['config'], reply=json.dumps({'answers': [reply_for(q, 'USD')]}))
    failed = call('preview', wrong['config'])
    assert not failed['ok'] and failed['output'] is None
    prompt = failed['repairPrompts'][0]
    assert prompt['sheet'] == 'input' and 'REPAIR A FAILED VALIDATION' in prompt['text']
    packet = json.loads(prompt['text'].split('### PACKET (answer this one)\n\n')[1].split('\n\n### YOUR REPLY')[0])
    assert any(b['target'] == 'currency' and b['args']['value'] == 'USD' for b in packet['current_bindings'])
    assert any(x['id'] == q['id'] for x in packet['questions'])
    assert packet['validation']['failed_checks'][0]['examples']['missing'][0]['row']['currency'] == 'EUR'
    assert 'Return entries only for decisions that need changing' in prompt['text']
    assert 'EVERY question id' not in prompt['text']
    fixed = call('answer', wrong['config'], reply='```json\n'+json.dumps({'answers': [reply_for(q, 'EUR')]})+'\n```')
    passed = call('preview', fixed['config'])
    assert passed['ok'] and not passed['repairPrompts']
    assert call('convert', fixed['config'])['ok']


def test_repair_can_correct_initially_confident_field_and_defer_to_manual(browser):
    req, draft = decision_draft(browser)
    # delivery_id was already bound by rules, so it has no initial question.
    assert all('delivery_id' not in q['targets'] for q in draft['questions'])
    qid = 'input:delivery_id'
    def answer(config, status, bindings):
        return json.loads(browser.execute(json.dumps({**req, 'action': 'answer', 'config': config,
            'reply': json.dumps({'answers': [{'question_id': qid, 'status': status, 'bindings': bindings}]})})))
    deferred = answer(draft['config'], 'unknown', [])
    assert any(q['id'] == qid for q in deferred['questions'])
    imported = json.loads(browser.execute(json.dumps({**req, 'action': 'load_config', 'config': deferred['config']})))
    assert any(q['id'] == qid for q in imported['questions'])
    with pytest.raises(ValueError, match='open question'):
        browser.execute(json.dumps({**req, 'action': 'convert', 'config': imported['config']}))
    fixed = answer(imported['config'], 'answered', [{'kind': 'column', 'target': 'delivery_id', 'args': {'source': 'delivery_id'}}])
    assert not any(q['id'] == qid for q in fixed['questions'])


def test_repair_unknown_reopens_saved_decision_without_approving_old_value(browser):
    req, draft = decision_draft(browser)
    q = draft['questions'][0]
    wrong = json.loads(browser.execute(json.dumps({**req, 'action': 'answer', 'config': draft['config'],
                      'reply': json.dumps({'answers': [reply_for(q, 'USD')]})})))
    assert not wrong['questions']
    unknown = {'question_id': q['id'], 'status': 'unknown', 'bindings': []}
    deferred = json.loads(browser.execute(json.dumps({**req, 'action': 'answer', 'config': wrong['config'],
                         'reply': json.dumps({'answers': [unknown]})})))
    assert any(x['id'] == q['id'] for x in deferred['questions'])
    assert deferred['config']['answers'][q['id']]['status'] == 'unknown'
    failed = json.loads(browser.execute(json.dumps({**req, 'action': 'preview', 'config': deferred['config']})))
    assert not failed['ok']
    packet = json.loads(failed['repairPrompts'][0]['text'].split('### PACKET (answer this one)\n\n')[1].split('\n\n### YOUR REPLY')[0])
    assert q['id'] in packet['unresolved_question_ids']


def test_expected_field_differences_use_unique_provenance_not_row_order(browser):
    import pandas as pd
    expected = pd.DataFrame({'source_sheet': ['Data', 'Data'], 'source_cell': ['B2', 'B3'], 'date': ['2022-11-02', '2022-10-26']})
    actual = expected.iloc[::-1].copy()
    actual['date'] = ['2022-10-22', '2022-10-29']
    result = browser.expected_control(expected, actual)
    assert not result['passed']
    diff = result['field_differences'][0]
    assert diff['source']['source_cell'] == 'B2'
    assert diff['fields']['date'] == {'expected': '2022-11-02T00:00:00', 'actual': '2022-10-29T00:00:00'}
    duplicate = pd.concat([expected, expected.iloc[[0]]], ignore_index=True)
    result = browser.expected_control(duplicate, duplicate.assign(date='2022-01-01'))
    assert not result['passed'] and 'field_differences' not in result


def test_repair_prompts_follow_affected_worksheet_and_clear_after_correction(browser):
    import pandas as pd
    with pd.ExcelWriter(browser.SESSION / 'input.xlsx') as writer:
        for sheet, identifier in [('First', 'A001'), ('Second', 'B001')]:
            pd.DataFrame({'id': [identifier], 'amount': [1]}).to_excel(writer, sheet_name=sheet, index=False)
    pd.DataFrame({'id': ['A001', 'B001'], 'amount': [1, 1], 'currency': ['EUR', 'EUR'],
                  'source_sheet': ['First', 'Second']}).to_csv(browser.SESSION / 'expected.csv', index=False)
    req = request(action='draft', input='input.xlsx', template='expected.csv', schema='', targetRole='expected')
    draft = json.loads(browser.execute(json.dumps(req)))
    qs = [q for q in draft['questions'] if q['targets'] == ['currency']]
    wrong = json.loads(browser.execute(json.dumps({**req, 'action': 'answer', 'config': draft['config'],
                      'reply': json.dumps({'answers': [reply_for(q, 'USD') for q in qs]})})))
    def preview(config):
        return json.loads(browser.execute(json.dumps({**req, 'action': 'preview', 'config': config})))
    failed = preview(wrong['config'])
    assert {p['sheet'] for p in failed['repairPrompts']} == {'First', 'Second'}
    for prompt in failed['repairPrompts']:
        packet = json.loads(prompt['text'].split('### PACKET (answer this one)\n\n')[1].split('\n\n### YOUR REPLY')[0])
        assert all(q['id'].startswith(prompt['sheet'] + ':') for q in packet['questions'])
        examples = packet['validation']['failed_checks'][0]['examples']['missing']
        assert all(x['row']['source_sheet'] == prompt['sheet'] for x in examples)
    for index, q in enumerate(qs):
        wrong = json.loads(browser.execute(json.dumps({**req, 'action': 'answer', 'config': wrong['config'],
                          'reply': json.dumps({'answers': [reply_for(q, 'EUR')]})})))
        result = preview(wrong['config'])
        assert [p['sheet'] for p in result['repairPrompts']] == ([qs[1]['frame']] if index == 0 else [])
    assert result['ok']


def test_guided_usda_review_requires_correct_business_choice(browser):
    """The POC's preset reply and manual choice drive the real engine, not canned results."""
    assets = ROOT / 'sites/playground/assets'
    fixture = json.loads((assets / 'review-usda.json').read_text(encoding='utf-8'))
    fixture['correction'] = json.loads((ROOT / 'sluice/tests/fixtures/review/usda-correction.json').read_text(encoding='utf-8'))
    shutil.copyfile(assets / 'review-usda.xlsx', browser.SESSION / 'input.xlsx')
    shutil.copyfile(assets / 'review-expected.csv', browser.SESSION / 'expected.csv')
    req = dict(input='input.xlsx', template='expected.csv', targetRole='expected', targetLayout='auto')

    def call(action, config, **extra):
        return json.loads(browser.execute(json.dumps({**req, 'action': action, 'config': config, **extra})))

    config = fixture['initialConfig']
    assert call('load_config', config)['config']['files'] == config['files']
    failed = call('preview', config)
    control = next(c for c in failed['report']['controls'] if c['name'] == 'expected_result')
    assert not failed['ok'] and control['dropped'] == 39
    automatic = [a for a in fixture['correction']['answers'] if not a['question_id'].endswith(':reporting_year')]
    config = call('answer', config, reply=json.dumps({'answers': automatic}))['config']
    assert not call('preview', config)['ok']
    year = next(a for a in fixture['correction']['answers'] if a['question_id'].endswith(':reporting_year'))
    deferred = call('answer', config, reply=json.dumps({'answers': [
        {'question_id': year['question_id'], 'status': 'unknown', 'bindings': []}
    ]}), manual=True)
    assert any(q['id'] == year['question_id'] for q in deferred['questions'])
    with pytest.raises(ValueError, match='open question'):
        call('convert', deferred['config'])
    original = fixture['initialConfig']['answers'][year['question_id']]
    wrong = call('answer', deferred['config'], reply=json.dumps({'answers': [
        {'question_id': year['question_id'], 'status': 'answered', 'bindings': original['bindings']}
    ]}), manual=True)
    assert not call('preview', wrong['config'])['ok']
    fixed = call('answer', wrong['config'], reply=json.dumps({'answers': [year]}), manual=True)
    assert call('preview', fixed['config'])['ok']
    # A JSON round trip simulates restoring the config from session storage; validation is fresh.
    restored = call('load_config', json.loads(json.dumps(fixed['config'])))['config']
    converted = call('convert', restored)
    assert converted['ok'] and Path(converted['output']).exists()
    got = browser.target_table(Path(converted['output']))
    expected = browser.target_table(browser.SESSION / 'expected.csv')
    assert len(got) == 54 and browser.expected_control(expected, got)['passed']


def test_usda_reply_stages_actual_changes_without_committing(browser):
    import copy
    assets = ROOT / 'sites/playground/assets'
    fixture = json.loads((assets / 'review-usda.json').read_text(encoding='utf-8'))
    fixture['correction'] = json.loads((ROOT / 'sluice/tests/fixtures/review/usda-correction.json').read_text(encoding='utf-8'))
    shutil.copyfile(assets / 'review-usda.xlsx', browser.SESSION / 'input.xlsx')
    shutil.copyfile(assets / 'review-expected.csv', browser.SESSION / 'expected.csv')
    config = fixture['initialConfig']
    original = copy.deepcopy(config)
    req = dict(input='input.xlsx', template='expected.csv', targetRole='expected', guided=True, config=config)
    def call(action, **extra):
        return json.loads(browser.execute(json.dumps({**req, 'action': action, **extra})))

    reply = '```json\n' + json.dumps(fixture['correction']) + '\n```'
    staged = call('prepare_reply', reply=reply)
    assert config == original and 'config' not in staged and 'output' not in staged
    assert len(staged['changes']) == 4 and staged['preview']['ok']
    assert staged['preview']['output'] is None and not staged['questions']
    assert staged['impact'] == dict(paired=True, rowsBefore=54, rowsAfter=54, changedRows=39, addedRows=0, removedRows=0)
    year = next(c for c in staged['changes'] if c['field'] == 'reporting_year')
    assert year['affectedRows'] == 12 and year['examples']
    # A different valid reply must produce different real suggestions, not the fixture correction.
    other = {'answers': [{'question_id': 'New Table 3:reporting_year', 'status': 'answered',
                         'bindings': [{'kind': 'constant', 'target': 'reporting_year',
                                       'args': {'value': '2023'}, 'confidence': .95}]}]}
    different = call('prepare_reply', reply=json.dumps(other))
    assert len(different['changes']) == 1 and different['changes'][0]['after'][0]['args']['value'] == '2023'
    assert different['changes'][0]['affectedRows'] == 54 and not different['preview']['ok']
    scope = {'answers': [{'question_id': 'New Table 3:grid_scope', 'status': 'answered',
                         'bindings': [{'kind': 'grid_scope', 'args': {'dimension': 'dim2',
                                       'include': ['Gulf', 'Texas Gulf', 'Northwest', 'East Gulf', 'Mexico3']}}]}]}
    filtered = call('prepare_reply', reply=json.dumps(scope))
    assert filtered['impact']['removedRows'] == 9 and filtered['impact']['rowsAfter'] == 45
    fixed = call('answer', reply=reply)['config']
    assert call('preview', config=fixed)['ok']
    assert call('preview', config=fixed)['reviewPrompts']  # Business feedback also works after checks pass.
    with pytest.raises(ValueError, match='does not change'):
        call('prepare_reply', config=fixed, reply=reply)


@pytest.mark.parametrize('status,confidence', [('unknown', 1), ('answered', .2)])
def test_staged_uncertain_reply_keeps_config_and_requires_followup(browser, status, confidence):
    req, draft = decision_draft(browser)
    q = draft['questions'][0]
    answered = reply_for(q, 'USD')
    original = json.loads(browser.execute(json.dumps({**req, 'action': 'answer', 'config': draft['config'],
                                                    'reply': json.dumps({'answers': [answered]})})))['config']
    answer = reply_for(q, 'EUR')
    answer['status'] = status
    answer['bindings'][0]['confidence'] = confidence
    staged = json.loads(browser.execute(json.dumps({**req, 'action': 'prepare_reply', 'config': original,
                                                   'reply': json.dumps({'answers': [answer]})})))
    assert staged['changes'][0]['status'] == 'unknown' and staged['questions']
    assert not staged['preview']['ok'] and staged['preview']['output'] is None
    assert original['answers'][q['id']]['bindings'][0]['args']['value'] == 'USD'
    rejected = json.loads(browser.execute_browser(json.dumps({**req, 'action': 'prepare_reply',
                           'config': original, 'reply': '{bad JSON'})))
    assert 'error' in rejected and 'Traceback' not in rejected['error']


def test_review_impact_does_not_pair_ambiguous_rows(browser):
    import pandas as pd
    before = pd.DataFrame({'source_cell': ['A1', 'A1'], 'value': [1, 2]})
    after = before.assign(value=[3, 4])
    changes = [{'field': 'value', 'sheet': 'Data'}]
    impact = browser.review_impact(before, after, changes, {'fields': [{'name': 'value', 'type': 'decimal'}]})
    assert not impact['paired'] and 'affectedRows' not in changes[0]
    unique = before.assign(source_cell=['A1', 'A2'])
    impact = browser.review_impact(unique, unique.iloc[::-1], changes, {'fields': [{'name': 'value', 'type': 'decimal'}]})
    assert impact['paired'] and impact['changedRows'] == 0 and changes[0]['affectedRows'] == 0
