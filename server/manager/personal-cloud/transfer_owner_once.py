"""Operator-only entry. Builds existing dependencies without starting API/reaper."""
import argparse,asyncio,json,re
from manager_kubernetes import build_from_env
from owner_transfer import transfer_once
async def main():
 p=argparse.ArgumentParser();p.add_argument('--transfer-once',action='store_true',required=True);p.add_argument('--owner',required=True);p.add_argument('--target-node',required=True);p.add_argument('--expected-generation',type=int,required=True);p.add_argument('--expected-handle-sha256',required=True);p.add_argument('--transfer-id',required=True);a=p.parse_args()
 report={'schema':'maimi-r94-normal-ap-transfer-controller/v1','status':'OUTCOME_UNCONFIRMED','automatic_retries':0,'automatic_rollback':False,'force_fencing':False,'employee_commands_replayed':0,'new_generation_started':False}
 try:
  if not re.fullmatch('[a-f0-9]{64}',a.expected_handle_sha256):raise ValueError('HANDLE_DIGEST_INVALID')
  # No ASGI lifespan, listener, reaper or background task is started here.
  manager=build_from_env().state.manager
  result=await transfer_once(manager,a.owner,a.target_node,expected_generation=a.expected_generation,expected_handle_sha256=a.expected_handle_sha256,transfer_id=a.transfer_id)
  report.update(result=result,status=result['status'])
 except BaseException as e:
  text=str(e);report['error']=text if re.fullmatch('[A-Z][A-Z0-9_]{0,80}',text) else type(e).__name__
  report['pending_intent_may_remain']=True
 print(json.dumps(report,separators=(',',':')),flush=True);return 0 if report['status']=='PLACEMENT_CHANGED_NEXT_OPEN_ALLOWED' else 2
if __name__=='__main__':raise SystemExit(asyncio.run(main()))
