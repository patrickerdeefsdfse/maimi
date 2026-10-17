'use strict';

const ORIGIN = 'http://127.0.0.1:28084';

function isNativeUrl(raw, origin = ORIGIN) {
  try {
    const url = new URL(raw);
    return url.origin === origin && url.protocol === new URL(origin).protocol && !url.username && !url.password;
  } catch { return false; }
}

function isExternalHttps(raw) {
  try {
    const url = new URL(raw);
    return url.protocol === 'https:' && !url.username && !url.password
      && !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
  } catch { return false; }
}

function isAllowedDownload(raw, origin = ORIGIN) {
  if (isNativeUrl(raw, origin)) return true;
  try {
    const url = new URL(raw);
    return url.protocol === 'blob:' && url.origin === origin
      && raw.startsWith(`blob:${origin}/`) && !url.search && !url.hash;
  } catch { return false; }
}

function isAllowedClipboardWrite(permission, requestingUrl, isMainFrame, origin) {
  return permission === 'clipboard-sanitized-write' && isMainFrame === true
    && isNativeUrl(requestingUrl, origin);
}

function isAllowedResource(raw, origin = ORIGIN) {
  if (isNativeUrl(raw, origin)) return true;
  try {
    const url = new URL(raw);
    return (url.protocol === 'ws:' && url.hostname === '127.0.0.1'
      && url.port === new URL(origin).port && !url.username && !url.password)
      || raw.startsWith(`blob:${origin}/`)
      || url.protocol === 'data:';
  } catch { return false; }
}

module.exports = { ORIGIN, isNativeUrl, isExternalHttps, isAllowedResource, isAllowedDownload, isAllowedClipboardWrite };
