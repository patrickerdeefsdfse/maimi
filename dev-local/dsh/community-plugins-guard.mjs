// MEGMEET adaptation for the pinned community release. The upstream files remain
// byte-for-byte intact; this module lives beside package.json in each package.
export * from './lib/host.js';
import * as upstream from './lib/host.js';

if (!Array.isArray(upstream.inject) || typeof upstream.apply !== 'function') {
  throw new Error('Unsupported community Host contract');
}
export const inject = [...new Set([...upstream.inject, 'connection'])];

export function apply(ctx, config) {
  const contexts = new WeakMap();
  function guardedContext(original) {
    if (contexts.has(original)) return contexts.get(original);
    const server = original.webServer;
    const connection = original.connection;
    if (typeof server?.register !== 'function' || typeof connection?.requestRejection !== 'function') {
      throw new Error('Native authenticated community routes are unavailable');
    }
    const guardedServer = new Proxy(server, {
      get(target, key) {
        if (key === 'register') return route => {
          if (route?.kind !== 'exact' || typeof route.path !== 'string' ||
              !route.path.startsWith('/ext/dshp-') || typeof route.handler !== 'function') {
            throw new Error('Unsupported community HTTP route');
          }
          const handler = route.handler;
          return target.register({ ...route, handler(request, response) {
            // The native Connection checks Host, Origin and its signed cookie.
            // Never fall back to an upstream handler on absent/failed auth.
            let rejection;
            try { rejection = connection.requestRejection(request); }
            catch { rejection = 503; }
            if (rejection !== undefined) {
              const status = rejection === 401 || rejection === 403 ? rejection : 503;
              response.writeHead(status, { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' });
              response.end(request.method === 'HEAD' ? undefined : 'community request denied');
              return;
            }
            return handler(request, response);
          } });
        };
        if (key === 'registerUpgrade' || key === 'registerFallback') return () => {
          throw new Error('Unsupported community transport');
        };
        const value = Reflect.get(target, key, target);
        return typeof value === 'function' ? value.bind(target) : value;
      },
    });
    const guarded = new Proxy(original, {
      get(target, key) {
        if (key === 'webServer') return guardedServer;
        if (key === 'get') return (name, ...args) => name === 'webServer'
          ? guardedServer : Reflect.apply(target.get, target, [name, ...args]);
        if (key === 'inject') return (...args) => {
          const wrapped = args.map(value => typeof value === 'function'
            ? (child, ...rest) => value(guardedContext(child), ...rest) : value);
          return Reflect.apply(target.inject, target, wrapped);
        };
        const value = Reflect.get(target, key, target);
        return typeof value === 'function' ? value.bind(target) : value;
      },
    });
    contexts.set(original, guarded);
    return guarded;
  }
  return upstream.apply(guardedContext(ctx), config);
}
