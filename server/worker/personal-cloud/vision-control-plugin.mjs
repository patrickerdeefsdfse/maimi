// Private model metadata only; the wrapper blocks /api/megmeet-cloud for clients.
// Pin to the reviewed alpha.2 session controller; no prompt/message is exported.
export const inject = ['sessionController', 'connection', 'fileUploads'];
// The pinned rc.2 session controller already indexes accepted request IDs, including
// removed inbox entries, while excluding copied history on a fork. Reuse its exact
// result before checking a consumed file receipt; no message data leaves the host.
export function apply(ctx) {
  ctx.effect(() => ctx.connection.fetch.register({
    path:'/api/megmeet-cloud/prompt-receipts', methods:['POST'], requestBody:'buffered',
    fetch:async request => {
      const {sessionId, requestId, receipts} = await request.json();
      if (typeof requestId !== 'string' || !/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(requestId)) return new Response('',{status:400});
      if (!/^(?:session-)?[0-9a-f-]{36}$/i.test(sessionId) || !Array.isArray(receipts) || receipts.length > 128 ||
          receipts.some(id => typeof id !== 'string' || !/^[a-f0-9-]{36}$/i.test(id))) return new Response('',{status:400});
      const result = await ctx.sessionController.resolveAgent(sessionId);
      if (!result.agent) return new Response('',{status:404});
      if (ctx.sessionController.commands.hasPromptRequest(result.agent.session,requestId)) return Response.json({accepted:true},{headers:{'Cache-Control':'no-store'}});
      let images = 0;
      for (const id of receipts) {
        const part = ctx.fileUploads.resolve(result.agent,id);
        if (!part) return new Response('',{status:404});
        if (part.type === 'image') images++;
      }
      return Response.json({images},{headers:{'Cache-Control':'no-store'}});
    }
  }), 'private prompt receipt kinds');
  ctx.effect(() => ctx.connection.fetch.register({
    path: '/api/megmeet-cloud/vision-selection', methods: ['GET'], requestBody: 'buffered',
    fetch: async request => {
      const values = new URL(request.url).searchParams.getAll('sessionId');
      if (values.length !== 1 || !/^(?:session-)?[0-9a-f-]{36}$/i.test(values[0]))
        return new Response('', {status: 400});
      const resolved = await ctx.sessionController.resolveAgent(values[0]);
      if ('error' in resolved) return new Response('', {status: 404});
      const selection = resolved.agent.model.current();
      if (selection?.provider !== 'deepseek-official' || typeof selection.model !== 'string'
        || !/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/.test(selection.model))
        return new Response('', {status: 409});
      return Response.json({protocol: 1, provider: selection.provider, model: selection.model},
        {headers: {'Cache-Control': 'no-store'}});
    }
  }), 'megmeet cloud vision selection metadata');
}
