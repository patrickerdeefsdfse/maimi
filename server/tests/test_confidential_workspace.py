import unittest
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
from app import confidential_policy as policy, personal_cloud as cloud, personal_model_proxy as models
from app.desktop_auth import TrustedActor
from app.iam_oauth import IAMFailure


class WorkspacePolicyTests(unittest.IsolatedAsyncioTestCase):
    ordinary = TrustedActor('QA', 'QA', 'QA', (), 'session')
    private = replace(ordinary, network_mode='confidential')

    def test_storage_namespace_is_stable_and_separate(self):
        from uuid import uuid5, NAMESPACE_URL
        self.assertEqual(cloud.personal_workspace_id(self.ordinary),
                         str(uuid5(NAMESPACE_URL, 'megmeet:personal-cloud:v1:QA')))
        self.assertNotEqual(cloud.owner_key(self.private), cloud.owner_key(self.ordinary))

    async def test_offline_endpoints_neither_consume_bytes_nor_forward(self):
        downstream = AsyncMock(side_effect=AssertionError('forwarded'))
        receive = AsyncMock(side_effect=AssertionError('consumed body'))
        for path in ('/api/session/uploadFileBinary', '/api/session/prompt','/v1/personal-cloud/open'):
            send = AsyncMock()
            scope = {'type':'http','method':'POST','path':policy.ConfidentialEntry.prefix + path}
            await policy.ConfidentialEntry(downstream)(scope, receive, send)
            self.assertEqual(send.await_args_list[0].args[0]['status'],503)
        receive.assert_not_awaited()
        downstream.assert_not_awaited()

    async def test_remote_model_denied_before_credential_lookup(self):
        broker = models.PersonalModelBroker.__new__(models.PersonalModelBroker)
        broker.base_url = 'https://untrusted.example.invalid/v1'
        broker.actor_validator = AsyncMock(side_effect=AssertionError('remote call'))
        with self.assertRaises(IAMFailure) as raised:
            await broker.models(self.private)
        self.assertEqual(raised.exception.code,'skill_runtime_unavailable')
        broker.actor_validator.assert_not_awaited()

    async def test_native_lease_denied_before_reading_file(self):
        with patch.object(cloud, 'actor_for', AsyncMock(return_value=self.private)):
            receive = AsyncMock(side_effect=AssertionError('consumed body'))
            request = Request({'type':'http','method':'POST','path':'/api/session/uploadFileBinary',
                               'query_string':b'', 'headers':[]},receive=receive)
            with self.assertRaises(IAMFailure) as raised:
                await cloud.native_http(request,'api/session/uploadFileBinary')
            self.assertEqual(raised.exception.code,'confidential_offline_runtime_required')
            receive.assert_not_awaited()

    async def test_explicit_protected_skill_stops_before_model_or_public_execution(self):
        with patch('app.skill_network_policy.resolve_skill_network_mode',return_value='confidential') as reviewed:
            body=[{'role':'user','content':[{'type':'text','text':'/private-qa check'}]}]
            with self.assertRaises(IAMFailure) as raised:
                await policy.check_model_turn(self.ordinary,body)
            self.assertEqual(raised.exception.code,'skill_runtime_unavailable')
            reviewed.assert_called_once_with({'private-qa'})
        with patch('app.skill_network_policy.resolve_skill_network_mode',return_value='public') as reviewed:
            await policy.check_model_turn(self.ordinary,[{'role':'user','content':'普通问候'}])
            reviewed.assert_not_called()
            await policy.check_model_turn(self.ordinary,[{'role':'user','content':'/public-qa check'}])
            reviewed.assert_called_once_with({'public-qa'})


if __name__=='__main__':
    unittest.main()
