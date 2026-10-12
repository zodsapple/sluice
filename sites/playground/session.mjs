// Session-local decisions only: no input bytes, outputs or validation approvals.
export async function pairKey(input, target, schema, options) {
  const digest = async bytes => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), b => b.toString(16).padStart(2, '0')).join('');
  const file = async f => f ? [f.name.split('.').pop().toLowerCase(), await digest(await f.arrayBuffer())] : null;
  return JSON.stringify([await file(input), await file(target), schema, options]);
}

export function remember(configs, key, config, limit = 10) {
  configs.delete(key); configs.set(key, structuredClone(config));
  while (configs.size > limit) configs.delete(configs.keys().next().value);
}
