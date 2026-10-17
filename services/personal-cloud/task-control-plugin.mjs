// Private, authenticated native metadata route. The wrapper and Gateway both
// deny this path to clients. No credentials or message content are serialized.
export const inject = ['agents','connection'];
export function apply(ctx) {
  let generation=0;
  const busy = agent => agent.status !== 'idle' || agent.inbox.nextTurn.length > 0
    || agent.inbox.nextStep.length > 0;
  ctx.effect(() => ctx.connection.fetch.register({
    path: '/api/megmeet-cloud/task-state', methods: ['GET','POST'], requestBody: 'buffered',
    fetch: async request => {
      if (request.method === 'POST') {
        let body;try{body=await request.json();}catch{return new Response('',{status:400});}
        if(body?.claim===true&&Number.isSafeInteger(body.generation)&&body.generation>0){
          if(body.generation<generation)return new Response('',{status:409});
          generation=body.generation;
        }else if(body?.cancel===true&&(body.generation===generation||(body.generation===undefined&&generation===0))){
          for (const agent of ctx.agents.list()) {
            for (const item of [...agent.inbox.nextTurn, ...agent.inbox.nextStep]) agent.inbox.remove(item.id);
            agent.cancel({kind:'user'});
          }
        }else{
          return new Response('',{status:409});
        }
      }
      const agents = ctx.agents.list();
      const jobs = ctx.get('jobs');
      const pendingJobs = agents.some(agent => jobs?.list(agent).some(job => ['running','stopping'].includes(job.status)));
      return Response.json({ protocol: 1, busy: agents.some(busy) || pendingJobs,
        activeRoots: ctx.agents.roots().filter(busy).map(agent => agent.id) },
        { headers: { 'Cache-Control': 'no-store' } });
    }
  }), 'megmeet cloud task authorization state');
}
