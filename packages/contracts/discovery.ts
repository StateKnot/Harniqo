import { contractValidator } from '@harniqo/contracts/validation';

export const authenticationDiscoveryPath = '/v1/auth/discovery';
export const authenticationDiscoveryNotConfigured = 'identity_not_configured';

export type AuthenticationDiscovery = Readonly<{
  issuer: string;
  client_id: string;
  required_scopes: readonly string[];
  authorization_code_pkce_required: true;
}>;

const validateDiscovery = contractValidator<AuthenticationDiscovery>('AuthenticationDiscovery');

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
