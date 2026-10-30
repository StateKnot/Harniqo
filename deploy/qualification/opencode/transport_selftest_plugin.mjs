import fs from 'node:fs';
import { runTests } from '../../../scripts/qualification/test_opencode_transport.mjs';

// Qualification only: invoked by the actual compiled Bun plugin loader.
// This tests fetch observation with local fixtures; it submits no model prompt.
export default async function selftestPlugin(_input, options) {
  if (options?.directory !== '/data/evidence') throw new Error('Invalid component evidence directory');
  return {
    async config() {
      const report = await runTests(options.directory);
      fs.writeFileSync(options.directory + '/transport-components.json', JSON.stringify(report, null, 2) + '\n',
        { mode: 0o600, flag: 'wx' });
      if (report.failed) throw new Error('OpenCode transport components failed');
    },
  };
}
