(() => {
  'use strict';
  const endpoint = '/v1/iam/web-activity';
  const request = window.fetch.bind(window);
  let timer, lastSent = 0, inFlight = false, touched = false, trailing;
  let confirmed = false, dialog;
  const passivePage = ['/maimi/', '/maimi/plugins/', '/maimi/monitor/'].includes(location.pathname);
  function expired() {
    if (dialog || (!confirmed && passivePage)) return;
    window.dispatchEvent(new Event('maimi:web-session-ended'));
    dialog = document.createElement('dialog');
    dialog.setAttribute('aria-labelledby', 'maimi-idle-title');
    const title = document.createElement('h2');
    title.id = 'maimi-idle-title'; title.textContent = '请重新登录麦米';
    const copy = document.createElement('p');
    copy.textContent = '当前网页版会话已结束。重新加载后可继续使用。';
    const button = document.createElement('button');
    button.type = 'button'; button.autofocus = true; button.textContent = '重新加载';
    button.onclick = () => {
      const dest = ['/maimi/monitor/', '/maimi/plugins/'].includes(location.pathname)
        ? location.pathname : '/maimi/web/';
      location.assign('/v1/iam/login?return_to=' + encodeURIComponent(dest));
    };
    dialog.style.cssText = 'position:fixed;inset:0;margin:auto;width:min(24rem,80vw);padding:1.6rem;color:#17352d;background:#fff;border:1px solid #cbdad5;border-radius:16px;box-shadow:0 12px 60px #092f3430;font:16px/1.6 system-ui';
    button.style.cssText = 'border:0;border-radius:8px;background:#226354;color:white;padding:.6rem 1.2rem;font:inherit;cursor:pointer';
    dialog.addEventListener('cancel', event => event.preventDefault());
    dialog.append(title, copy, button);
    document.body.append(dialog); dialog.showModal(); button.focus();
  }
  async function check() {
    if (dialog || inFlight) return;
    clearTimeout(timer);
    inFlight = true;
    const active = touched;
    if (active) lastSent = Date.now();
    touched = false;
    try {
      const response = await request(endpoint, {method:active?'POST':'GET', credentials:'same-origin',
        cache:'no-store', signal:AbortSignal.timeout(8000)});
      if (response.status === 401) { expired(); return; }
      if (!response.ok) throw Error();
      const {remaining_seconds: remaining} = await response.json();
      if (!Number.isInteger(remaining) || remaining < 0 || remaining > 7200) throw Error();
      confirmed = true;
      if (active) lastSent = Date.now();
      timer = setTimeout(check, Math.max(1, remaining) * 1000);
    } catch {
      if (active) touched = true;
      timer = setTimeout(check, 10000);
    } finally {
      inFlight = false;
      if (touched && !dialog) schedule();
    }
  }
  // A resource 401 can be from a workspace or plugin. A passive IAM check
  // settles whether this browser needs to sign in, without extending its idle deadline.
  let verification, lastVerification = -Infinity;
  async function verify() {
    if (dialog || verification || performance.now() - lastVerification < 5000) return;
    lastVerification = performance.now();
    verification = true;
    try {
      const response = await request(endpoint, {method:'GET', credentials:'same-origin',
        cache:'no-store', signal:AbortSignal.timeout(8000)});
      if (response.status === 401) expired();
    } catch { /* Transport failure leaves the ordinary idle check in charge. */ }
    finally { verification = false; }
  }
  function schedule() {
    clearTimeout(trailing);
    trailing = setTimeout(check, Math.max(0, 15000 - (Date.now() - lastSent)));
  }
  function activity(event) {
    if (event.isTrusted && !dialog && (confirmed || !passivePage)) {
      touched = true; schedule();
    }
  }
  const watched = new WeakSet();
  function watch(doc) {
    if (!doc || watched.has(doc)) return;
    watched.add(doc);
    for (const event of ['keydown','pointerdown','wheel','input','touchstart'])
      doc.addEventListener(event,activity,{capture:true,passive:true});
    function frame(node) { try { watch(node.contentDocument); } catch {} }
    doc.addEventListener('load',e=>{if(e.target?.tagName==='IFRAME')frame(e.target);},true);
    for (const node of doc.querySelectorAll('iframe')) frame(node);
  }
  function start() {
    watch(document);
    window.addEventListener('maimi:verify-web-session', verify);
    window.addEventListener('pageshow', check);
    window.addEventListener('online', check);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) void check(); });
    void check();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once:true});
  else start();
})();
