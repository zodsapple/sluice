import { test } from 'node:test';
import assert from 'node:assert/strict';
import { pairKey, remember } from '../sites/playground/session.mjs';
test('File identity uses content and format, never a display filename', async () => {
  const options = {role: 'expected', layout: 'auto', sheets: ''};
  const a = new File(['id\n001\n'], 'input.csv'), b = new File(['id\n001\n'], 'renamed.csv');
  const expected = new File(['id\n001\n'], 'expected.csv');
  const key = await pairKey(a, expected, '', options);
  assert.equal(key, await pairKey(b, expected, '', options));
  assert.notEqual(key, await pairKey(new File(['id\n002\n'], 'input.csv'), expected, '', options));
  assert.notEqual(key, await pairKey(a, new File(['id\n002\n'], 'expected.csv'), '', options));
  assert.notEqual(key, await pairKey(a, expected, '', {...options, role: 'template'}));
  assert.notEqual(key, await pairKey(a, expected, '{"fields":[]}', options));
});
test('Cached decisions are isolated, bounded and serializable across tab reloads', () => {
  const cache = new Map(), config = {spec: {currency: 'EUR'}, answers: {a: {value: 'EUR'}}};
  remember(cache, 'pair', config, 2); config.spec.currency = 'USD';
  assert.equal(cache.get('pair').spec.currency, 'EUR');
  const restored = new Map(JSON.parse(JSON.stringify([...cache])));
  assert.deepEqual(restored.get('pair').answers, {a: {value: 'EUR'}});
  remember(cache, 'second', config, 2); remember(cache, 'pair', config, 2); remember(cache, 'third', config, 2);
  assert.equal(cache.has('second'), false); assert.equal(cache.has('pair'), true);
  assert.equal('validated' in cache.get('pair'), false);
});
