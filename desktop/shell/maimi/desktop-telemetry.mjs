// Only version metadata leaves the desktop. The existing company grant binds
// the employee and device server-side; heartbeat deliberately does not emit DAU.
const VERSION = /^[A-Za-z0-9.+_-]{1,64}$/;
export function startDesktopTelemetry({version, getAuthorization, request,
  intervalMs=60_000, timeoutMs=5_000, setTimer=setTimeout, clearTimer=clearTimeout,
  now=Date.now}={}) {
  if (!VERSION.test(version ?? '') || typeof getAuthorization !== 'function' || typeof request !== 'function') {
    throw Error('DESKTOP_TELEMETRY_ARGUMENTS');
  }
  if (!Number.isFinite(intervalMs) || intervalMs < 60_000 || !Number.isFinite(timeoutMs) || timeoutMs < 1_000 || timeoutMs > 10_000) {
    throw Error('DESKTOP_TELEMETRY_INTERVAL');
  }
  let stopped=false, timer, active, failures=0, lastStatus='scheduled';
  function schedule(delay) {
    if (stopped) return;
    timer=setTimer(tick,delay); timer?.unref?.();
  }
  async function tick() {
    timer=undefined;
    if (stopped || active) return;
    let auth;
    try { auth=getAuthorization(); } catch { lastStatus='awaiting_sign_in'; schedule(intervalMs); return; }
    if (!auth || typeof auth.token!=='string' || !auth.token || !Number.isFinite(auth.expiresAt) || auth.expiresAt<=now()) {
      lastStatus='awaiting_sign_in'; schedule(intervalMs); return;
    }
    const controller=new AbortController(); active=controller;
    const deadline=setTimer(()=>controller.abort(),timeoutMs); deadline?.unref?.();
    try {
      const reply=await request('/v1/desktop/auth/telemetry',{token:auth.token,body:{version},signal:controller.signal});
      if (reply?.status===200 && reply?.value?.status==='recorded') {
        failures=0; lastStatus='recorded';
      } else { failures++; lastStatus='unavailable'; }
    } catch { failures++; lastStatus=stopped?'stopped':'unavailable'; }
    finally {
      clearTimer(deadline); active=undefined;
      schedule(Math.min(intervalMs * 2**Math.min(failures,4),15*60_000));
    }
  }
  schedule(1_000);
  return Object.freeze({stop(){stopped=true; clearTimer(timer); active?.abort(); lastStatus='stopped';},
    status(){return {state:lastStatus,enabled:!stopped};}});
}
