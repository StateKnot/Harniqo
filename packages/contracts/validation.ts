import { Ajv2020 } from 'ajv/dist/2020.js';
import type { ValidateFunction } from 'ajv';
import schema from './schemas/contract.schema.json' with { type: 'json' };

const ajv = new Ajv2020({ strictTypes: false, strictRequired: false });
ajv.addFormat('uuid', /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/u);
ajv.addFormat('date-time', (value: string) => {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$/u.test(value) || value.startsWith('0000-')) return false;
  const instant = new Date(value);
  return Number.isFinite(instant.getTime()) && instant.toISOString().slice(0, 19) === value.slice(0, 19);
});
ajv.addFormat('uri', {
  type: 'string',
  validate: (value: string) => {
    if (!/^[a-z][a-z0-9+.-]*:\/\/[^/?#@\\\s]+/iu.test(value)
      || value.split(/[/?#]/u, 3)[2]?.includes('@') || /[\p{Cc}\\\s]/u.test(value)) return false;
    const url = URL.parse(value);
    return !!url?.hostname && !url.username && !url.password && !/\s/u.test(value);
  },
});
ajv.addSchema(schema);

export function contractValidator<T = unknown>(definition: keyof typeof schema.$defs): ValidateFunction<T> {
  const ref = schema.$id + '#/$defs/' + definition;
  return ajv.getSchema<T>(ref) ?? ajv.compile<T>({ $ref: ref });
}
