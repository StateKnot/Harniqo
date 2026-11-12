#!/usr/bin/env node
import { spawn } from 'node:child_process';
import { createHash, X509Certificate } from 'node:crypto';
import { once } from 'node:events';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const authorization = new URL(process.argv[2]);
const issuer = new URL(process.env.HARNIQO_TEST_ISSUER);
const username = process.env.HARNIQO_TEST_USERNAME;
const password = (await readFile(process.env.HARNIQO_TEST_PASSWORD_FILE, 'utf8')).trimEnd();
const certificate = new X509Certificate(await readFile(process.env.HARNIQO_TEST_CERT));
if (authorization.origin !== issuer.origin || !username || !password) throw new Error('Invalid browser qualification input');
const pin = createHash('sha256').update(certificate.publicKey.export({ type: 'spki', format: 'der' })).digest('base64');
const chrome = process.env.HARNIQO_TEST_CHROME || (process.platform === 'darwin'
  ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' : 'chromium');
const profile = await mkdtemp(path.join(tmpdir(), 'harniqo-oidc-browser-'));
const browser = spawn(chrome, [
  '--headless', '--no-first-run', '--no-default-browser-check', '--disable-background-networking',
  '--remote-debugging-port=0', '--user-data-dir=' + profile,
  '--ignore-certificate-errors-spki-list=' + pin, 'about:blank',
], { stdio: 'ignore' });
const browserDone = new Promise(resolve => {
  browser.once('error', resolve);
  browser.once('exit', resolve);
});
const deadline = Date.now() + 13000;
let connection;
let nextId = 0;
let stopped = false;
const stop = () => { stopped = true; browser.kill('SIGTERM'); };
process.once('SIGTERM', stop);
process.once('SIGINT', stop);

async function waitFor(read, label) {
  while (!stopped && Date.now() < deadline && browser.exitCode === null && browser.signalCode === null) {
    const value = await read();
    if (value) return value;
    await delay(100);
  }
  throw new Error('Browser qualification did not reach ' + label);
}

function cdp(method, params) {
  const id = ++nextId;
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      connection.removeEventListener('message', onMessage);
      reject(new Error('Browser debugger response timed out'));
    }, 1500);
    function onMessage(event) {
      const response = JSON.parse(event.data);
      if (response.id !== id) return;
      clearTimeout(timeout);
      connection.removeEventListener('message', onMessage);
      if (response.error) reject(new Error('Browser debugger request failed'));
      else resolve(response.result);
    }
    connection.addEventListener('message', onMessage);
    connection.send(JSON.stringify({ id, method, params }));
  });
}

try {
  const port = await waitFor(async () => {
    try { return Number((await readFile(path.join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]); }
    catch { return 0; }
  }, 'debugger startup');
  const pages = await (await fetch('http://127.0.0.1:' + port + '/json/list', { signal: AbortSignal.timeout(1500) })).json();
  connection = new WebSocket(pages.find(page => page.type === 'page').webSocketDebuggerUrl);
  await once(connection, 'open', { signal: AbortSignal.timeout(1500) });
  const navigation = await cdp('Page.navigate', { url: authorization.href });
  if (navigation.errorText) throw new Error('Browser authorization page failed to load');
  const loginForm = `location.origin === ${JSON.stringify(issuer.origin)}
    && Boolean(document.querySelector('#kc-form-login #username') && document.querySelector('#kc-form-login #password'))`;
  await waitFor(async () => {
    const value = await cdp('Runtime.evaluate', { expression: loginForm, returnByValue: true });
    return value.result?.value === true;
  }, 'the trusted Keycloak login page');
  await cdp('Runtime.evaluate', { expression: `(() => {
    if (!(${loginForm})) throw new Error('Login page changed');
    document.querySelector('#kc-form-login #username').value = ${JSON.stringify(username)};
    document.querySelector('#kc-form-login #password').value = ${JSON.stringify(password)};
    document.querySelector('#kc-form-login').requestSubmit();
  })()` });
  const callback = new URL(authorization.searchParams.get('redirect_uri'));
  await waitFor(async () => {
    const value = await cdp('Runtime.evaluate', {
      expression: `location.origin === ${JSON.stringify(callback.origin)} && location.pathname === ${JSON.stringify(callback.pathname)}`,
      returnByValue: true,
    });
    return value.result?.value === true;
  }, 'the desktop loopback callback');
} finally {
  process.removeListener('SIGTERM', stop);
  process.removeListener('SIGINT', stop);
  connection?.close();
  if (browser.exitCode === null && browser.signalCode === null) browser.kill('SIGTERM');
  const exited = await Promise.race([browserDone.then(() => true), delay(1200, false)]);
  if (!exited) {
    browser.kill('SIGKILL');
    await browserDone;
  }
  await rm(profile, { force: true, recursive: true });
}
