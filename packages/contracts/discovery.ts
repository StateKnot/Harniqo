import { Ajv2020 } from 'ajv/dist/2020.js';
import schema from './schemas/contract.schema.json' with { type: 'json' };

export const authenticationDiscoveryPath = '/v1/auth/discovery';
export const authenticationDiscoveryNotConfigured = 'identity_not_configured';

export type AuthenticationDiscovery = Readonly<{
  issuer: string;
  client_id: string;
  required_scopes: readonly string[];
  authorization_code_pkce_required: true;
}>;

const ajv = new Ajv2020();
ajv.addFormat('uri', {
  type: 'string',
  validate: (value: string) => {
    const url = URL.parse(value);
    return !!url?.hostname && !url.username && !url.password && !/\s/u.test(value);
  },
});
ajv.addSchema(schema);
const validateDiscovery = ajv.compile<AuthenticationDiscovery>({
  $ref: schema.$id + '#/$defs/AuthenticationDiscovery',
});

export function parseAuthenticationDiscovery(value: unknown): AuthenticationDiscovery {
  if (!validateDiscovery(value)) throw new Error('invalid_discovery');
  const issuer = URL.parse(value.issuer);
  const authority = value.issuer.slice('https://'.length).split('/')[0];
  if (!issuer?.hostname || issuer.protocol !== 'https:' || authority?.includes('@')
    || /[?#\\\u0000-\u0020\u007f]/u.test(value.issuer)
    || !value.required_scopes.includes('openid')
    || value.required_scopes.some(scope => !/^[\x21\x23-\x5B\x5D-\x7E]+$/u.test(scope))) {
    throw new Error('invalid_discovery');
  }
  return Object.freeze({
    issuer: value.issuer,
    client_id: value.client_id,
    required_scopes: Object.freeze([...value.required_scopes]),
    authorization_code_pkce_required: true,
  });
}
