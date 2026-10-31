import { BlockList, isIP } from 'node:net';
import { networkInterfaces } from 'node:os';
import ipaddr from 'ipaddr.js';
import type { Address } from './types.ts';

// Conservative IANA special-purpose exclusions, including special allocations
// that are globally reachable. This does not discover deployment-specific NAT64.
const V4_DENY = ['0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
  '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24',
  '192.31.196.0/24', '192.52.193.0/24', '192.88.99.0/24', '192.168.0.0/16',
  '192.175.48.0/24', '198.18.0.0/15', '198.51.100.0/24', '203.0.113.0/24',
  '224.0.0.0/4', '240.0.0.0/4'];
const V6_DENY = ['2001::/23', '2001:db8::/32', '2002::/16',
  '2620:4f:8000::/48', '3ffe::/16', '3fff::/20'];

export class PolicyError extends Error {
  readonly code: string;
  constructor(code: string) { super(code); this.code = code; }
}

export function canonicalAddress(value: string): Address {
  if (typeof value !== 'string' || value.includes('%') || isIP(value) === 0) {
    throw new PolicyError('address_invalid');
  }
  const parsed = ipaddr.parse(value);
  if (parsed.kind() === 'ipv6' && (parsed as ipaddr.IPv6).isIPv4MappedAddress()) {
    throw new PolicyError('address_mapped_ipv6');
  }
  return { address: parsed.toString(), family: parsed.kind() === 'ipv4' ? 4 : 6 };
}

function addCidr(list: BlockList, value: string): void {
  if (typeof value !== 'string' || !value.includes('/') || value.includes('%')) {
    throw new PolicyError('denied_cidr_invalid');
  }
  try {
    const input = value.split('/');
    if (input.length !== 2 || !input[0] || !input[1] || !/^[0-9]+$/.test(input[1])) throw new PolicyError('denied_cidr_invalid');
    canonicalAddress(input[0]);
    const [address, prefix] = ipaddr.parseCIDR(value);
    if (address.kind() === 'ipv6' && (address as ipaddr.IPv6).isIPv4MappedAddress()) {
      throw new PolicyError('denied_cidr_invalid');
    }
    list.addSubnet(address.toString(), prefix, address.kind());
  } catch {
    throw new PolicyError('denied_cidr_invalid');
  }
}

const special = new BlockList();
for (const value of [...V4_DENY, ...V6_DENY]) addCidr(special, value);
const exceptionNetworks = new BlockList();
for (const value of ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '127.0.0.0/8', '::1/128', 'fc00::/7']) addCidr(exceptionNetworks, value);
const publicV6 = new BlockList();
publicV6.addSubnet('2000::', 3, 'ipv6');

export function isPublicAddress(value: Address): boolean {
  const family = value.family === 4 ? 'ipv4' : 'ipv6';
  return !special.check(value.address, family)
    && (value.family === 4 || publicV6.check(value.address, family));
}

export class AddressPolicy {
  private readonly denied = new BlockList();
  constructor(deniedCidrs: readonly string[]) {
    for (const value of deniedCidrs) addCidr(this.denied, value);
  }
  private deniedAddress(value: Address): boolean {
    return this.denied.check(value.address, value.family === 4 ? 'ipv4' : 'ipv6');
  }
  exceptions(values: readonly string[]): ReadonlySet<string> {
    if (!Array.isArray(values) || values.length > 32) throw new PolicyError('private_exception_invalid');
    const result = new Set<string>();
    for (const item of values) {
      const value = canonicalAddress(item);
      if (!exceptionNetworks.check(value.address, value.family === 4 ? 'ipv4' : 'ipv6') || this.deniedAddress(value)) throw new PolicyError('private_exception_invalid');
      result.add(value.address);
    }
    return result;
  }
  checkAll(values: readonly Address[], exceptions: ReadonlySet<string>): Address {
    if (!Array.isArray(values) || values.length === 0 || values.length > 64) {
      throw new PolicyError('dns_answer_invalid');
    }
    const local = new Set<string>();
    for (const entries of Object.values(networkInterfaces())) {
      for (const entry of entries ?? []) {
        try { local.add(canonicalAddress(entry.address).address); } catch { /* Non-IP OS entries cannot be dialled. */ }
      }
    }
    const checked: Address[] = [];
    for (const raw of values) {
      if (!raw || (raw.family !== 4 && raw.family !== 6)) throw new PolicyError('dns_answer_invalid');
      const value = canonicalAddress(raw.address);
      if (value.family !== raw.family) throw new PolicyError('dns_answer_invalid');
      if (this.deniedAddress(value)
          || (!exceptions.has(value.address) && (!isPublicAddress(value) || local.has(value.address)))) {
        throw new PolicyError('address_denied');
      }
      checked.push(value);
    }
    // One attempt only, without a hidden second resolver or automatic retry.
    return checked.find(value => value.family === 4) ?? checked[0]!;
  }
}
