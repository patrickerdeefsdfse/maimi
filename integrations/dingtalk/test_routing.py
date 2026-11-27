import dataclasses
import unittest

from routing import Binding, Channel, ReceivedText, Refused, dingtalk_text, resolve


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.now=2_000_000_000
        self.channel=Channel("dingtalk","synthetic-bot","synthetic-corp")
        self.event=dingtalk_text(self.channel,{
            "chatbotCorpId":"synthetic-corp","senderCorpId":"synthetic-corp","robotCode":"synthetic-bot",
            "senderStaffId":"subject","msgId":"message","conversationId":"dialog","conversationType":"1",
            "msgtype":"text","text":{"content":"synthetic"},"createAt":self.now*1000})
        self.binding=Binding(self.channel,"subject","M92000001","epoch-1",self.now-100,self.now+7200,True)
    def checked(self,event=None,bindings=None,active=True):
        return resolve(event or self.event, [self.binding] if bindings is None else bindings,
                       lambda owner:active, now=self.now,routing_key=b'0'*32)
    def test_receipt_stable_even_if_grant_rotates_but_session_separates(self):
        a=self.checked();b=self.checked(bindings=[dataclasses.replace(self.binding,generation="epoch-2")])
        self.assertEqual(a.receipt_key,b.receipt_key)
        self.assertNotEqual(a.conversation_key,b.conversation_key)
    def test_groups_commands_artifacts_and_old_events_refused(self):
        for patch in ({"direct":False},{"text":"  /workspace /somewhere"},{"text":None},{"created_at":self.now-301}):
            with self.subTest(**patch),self.assertRaises(Refused):
                self.checked(dataclasses.replace(self.event,**patch))
    def test_revoked_duplicate_stale_disallowed(self):
        for bindings in ([],[self.binding,self.binding],[dataclasses.replace(self.binding,enabled=False)],
                         [dataclasses.replace(self.binding,valid_until=self.now)],
                         [dataclasses.replace(self.binding,valid_since=self.now+2)]):
            with self.assertRaises(Refused):
                self.checked(bindings=bindings)
        with self.assertRaises(Refused):
            self.checked(active=False)
    def test_channel_scopes_and_sender_checked(self):
        for event in (dataclasses.replace(self.event,sender="other"),
                      dataclasses.replace(self.event,channel=Channel("dingtalk","other","synthetic-corp")),
                      dataclasses.replace(self.event,channel=Channel("weixin","synthetic-bot","synthetic-corp"))):
            with self.assertRaises(Refused):
                self.checked(event)
    def test_weixin_explicit_binding_per_installation(self):
        channel=Channel("weixin","synthetic-bot","installation-a")
        event=dataclasses.replace(self.event,channel=channel)
        with self.assertRaises(Refused):
            self.checked(event)
        route=self.checked(event,[dataclasses.replace(self.binding,channel=channel)])
        self.assertNotEqual(self.checked().conversation_key,route.conversation_key)
    def test_dingtalk_foreign_tenant_and_sender_fallback_refused(self):
        template=dict(robotCode="synthetic-bot",chatbotCorpId="synthetic-corp",senderCorpId="synthetic-corp",conversationType="1",msgtype="text",createAt=self.now*1000,
                      senderStaffId="subject",senderId="different",conversationId="dialog",msgId="event",text={"content":"test"})
        for patch in ({"senderCorpId":"other"},{"chatbotCorpId":"other"},{"robotCode":"other"},
                      {"senderStaffId":None},{"conversationType":"2"},{"msgtype":"picture"}):
            with self.assertRaises(Refused):
                dingtalk_text(self.channel,template|patch)

if __name__ == "__main__":
    unittest.main()

