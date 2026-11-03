import type { DesktopInterface } from '../shared/interface';

declare global {
  interface Window { readonly harniqo: DesktopInterface; }
}
