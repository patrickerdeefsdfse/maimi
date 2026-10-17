// Private model metadata only; the wrapper blocks /api/megmeet-cloud for clients.
// Pin to the reviewed alpha.2 session controller; no prompt/message is exported.
export const inject = ['sessionController', 'connection'];
export function apply(ctx) {
  ctx.effect(() => ctx.connection.fetch.register({
    path: '/api/megmeet-cloud/vision-selection', methods: ['GET'], requestBody: 'buffered',
    fetch: async request => {
      const values = new URL(request.url).searchParams.getAll('sessionId');
      if (values.length !== 1 || !/^(?:session-)?[0-9a-f-]{36}$/i.test(values[0]))
        return new Response('', {status: 400});
      const resolved = await ctx.sessionController.resolveAgent(values[0]);
      if ('error' in resolved) return new Response('', {status: 404});
      const selection = ctx.sessionController.agents.selectionFor(resolved.agent).current;
      if (selection?.provider !== 'deepseek-official' || typeof selection.model !== 'string'
        || !/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/.test(selection.model))
        return new Response('', {status: 409});
      return Response.json({protocol: 1, provider: selection.provider, model: selection.model},
        {headers: {'Cache-Control': 'no-store'}});
    }
  }), 'megmeet cloud vision selection metadata');
}
