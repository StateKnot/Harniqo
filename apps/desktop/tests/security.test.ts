import assert from 'node:assert/strict';
import { test } from 'node:test';
import { APP_URL, assertDesktopRequest, resolveAssetPath } from '../src/main/policy';

const assets = new Set(['/index.html', '/assets/index-B3qx.js', '/assets/index-L4zz.css']);

test('only exact built renderer assets can be requested; callers cannot choose host paths or protocols', () => {
  assert.equal(resolveAssetPath(APP_URL, 'GET', assets), '/index.html');
  assert.equal(resolveAssetPath('harniqo://app/assets/index-B3qx.js', 'GET', assets), '/assets/index-B3qx.js');
  const denied = [
    'file:///etc/passwd', 'https://app/index.html', 'http://localhost:5173/index.html',
    'data:text/html,<script>alert(1)</script>', 'javascript:alert(1)',
    'harniqo://evil/index.html', 'harniqo://app.evil/index.html', 'harniqo://user@app/index.html',
    'harniqo://app:80/index.html', 'harniqo://app/index.html?path=/etc/passwd',
    'harniqo://app/index.html#x', 'harniqo://app/assets/../index.html',
    'harniqo://app/../../etc/passwd', 'harniqo://app/%2e%2e/etc/passwd',
    'harniqo://app/%69ndex.html', 'harniqo://app/assets%2findex-B3qx.js',
    'harniqo://app/assets\\index-B3qx.js', 'harniqo://app//index.html',
    'harniqo://app/main/main.js', 'harniqo://app/build-info.json',
    'harniqo://app/assets/unknown.js', 'not a url',
  ];
  for (const url of denied) assert.equal(resolveAssetPath(url, 'GET', assets), undefined, url);
  for (const method of ['POST', 'PUT', 'DELETE', 'HEAD']) assert.equal(resolveAssetPath(APP_URL, method, assets), undefined, method);
});

test('compromised subframes or other windows cannot read through the main window IPC capability', () => {
  const contents = {};
  const mainFrame = { url: APP_URL };
  assert.doesNotThrow(() => assertDesktopRequest({ sender: contents, senderFrame: mainFrame }, [], contents, mainFrame));
  const attackers = [
    { sender: {}, senderFrame: mainFrame },
    { sender: contents, senderFrame: { url: APP_URL } },
    { sender: contents, senderFrame: null },
    { sender: contents, senderFrame: { url: 'https://example.com' } },
  ];
  for (const event of attackers) assert.throws(() => assertDesktopRequest(event, [], contents, mainFrame), /DESKTOP_REQUEST_DENIED/);
  mainFrame.url = 'harniqo://app/assets/index-B3qx.js';
  assert.throws(() => assertDesktopRequest({ sender: contents, senderFrame: mainFrame }, [], contents, mainFrame), /DESKTOP_REQUEST_DENIED/);
});

test('read-only runtime IPC cannot be repurposed as shell, URL, or file access', () => {
  const contents = {};
  const frame = { url: APP_URL };
  const event = { sender: contents, senderFrame: frame };
  for (const args of [[undefined], [null], [{ path: '/etc/passwd' }], [{ command: 'sh', args: ['-c', 'id'] }], ['https://example.com'], ['getRuntimeInfo'], [[], {}]]) {
    assert.throws(() => assertDesktopRequest(event, args, contents, frame), /DESKTOP_REQUEST_DENIED/);
  }
});

test('the probe channel accepts exactly one argument from the local top frame', () => {
  const contents = {};
  const frame = { url: APP_URL };
  const event = { sender: contents, senderFrame: frame };
  assert.doesNotThrow(() => assertDesktopRequest(event, ['https://example.com'], contents, frame, 1));
  assert.throws(() => assertDesktopRequest(event, ['https://example.com', { ca: '/etc/passwd' }], contents, frame, 1), /DESKTOP_REQUEST_DENIED/);
  assert.throws(() => assertDesktopRequest({ ...event, senderFrame: { url: APP_URL } }, ['https://example.com'], contents, frame, 1), /DESKTOP_REQUEST_DENIED/);
});
