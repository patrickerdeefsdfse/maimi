"""Private MES region/factory context; all phone and address values synthetic."""
import json
import unittest

import httpx

from test_hosted_eta import q, Server, query, ORDER, WAYBILL


def policy_data():
    return {"schema": 1, "factory_aliases": {
        "瑞欣特": "ruixinte", "埃斯蒙特": "aisimengte", "硕特科技": "shuote"},
        "ruixinte_sender_phone": "13900009187", "shuote_kuayue_phone": "13900008276"}


def policy():
    return q.FactoryPhonePolicy.from_private_data(policy_data())


class PhoneServer(Server):
    def __init__(self):
        super().__init__()
        self.mes_calls = 0

    async def __call__(self, request):
        request_body = json.loads(request.content)
        if str(request.url) == q.MES_ENDPOINT:
            self.mes_calls += 1
        response = await super().__call__(request)
        if request_body.get('method') == 'tools/call' and request_body['params']['name'] == 'query_trace':
            body = json.loads(response.content)
            payload = body['result']['structuredContent']
            number = payload['kuaidiNum']
            carrier = 'shunfeng' if number.startswith('SF') else 'kuayue' if number.startswith('KY') else 'debangkuaidi'
            payload.update(kuaidiCom=carrier, state='301', fromTo='合成起点 -> ')
            payload['data'][0].update(status='301', context='合成签收动态')
            return httpx.Response(200, json=body)
        return response


class FactoryPolicyTests(unittest.TestCase):
    def test_private_policy_is_reduced_exact_and_not_represented(self):
        configured = policy()
        self.assertEqual(configured.ruixinte_sender_suffix, '9187')
        self.assertEqual(configured.shuote_kuayue_phone, '13900008276')
        self.assertNotIn('9187', repr(configured))
        self.assertNotIn('13900008276', repr(configured))
        self.assertNotIn('13900008276', repr(q.LogisticsConfig(factory_phone_policy=configured)))

    def test_private_policy_rejects_wrong_types_phones_and_unapproved_aliases(self):
        bad = []
        for key, value in [('schema', True), ('schema', 2), ('ruixinte_sender_phone', 'masked***'),
                           ('ruixinte_sender_phone', None), ('shuote_kuayue_phone', '8276'),
                           ('shuote_kuayue_phone', '13900008276,13900009187')]:
            candidate = policy_data(); candidate[key] = value; bad.append(candidate)
        for aliases in ({'瑞欣特': []}, {'瑞欣特有限公司': 'ruixinte'}, {'蓝色河谷': 'ruixinte'},
                        {' 瑞欣特 ': 'ruixinte'}, {},
                        {'瑞欣特': 'ruixinte', '埃斯蒙特': 'aisimengte', '硕特': 'shuote'}):
            candidate = policy_data(); candidate['factory_aliases'] = aliases; bad.append(candidate)
        for candidate in bad:
            with self.subTest(keys=list(candidate)), self.assertRaisesRegex(q.LogisticsQueryError, 'INVALID_SERVER_CONFIGURATION'):
                q.FactoryPhonePolicy.from_private_data(candidate)

    def test_country_prefix_is_removed_only_from_valid_complete_mobile(self):
        value = policy_data(); value['shuote_kuayue_phone'] = '+86 139-0000-8276'
        self.assertEqual(q.FactoryPhonePolicy.from_private_data(value).shuote_kuayue_phone, '13900008276')

    def test_context_is_private_and_multi_waybill_destination_is_not_assumed(self):
        source = {'出货工厂': ' 蓝色河谷 ', '发货通知人手机号码': '13900007654',
                  '收货地址': '广东省惠州惠阳区某镇某街99号 收件人合成人 电话13900007654'}
        context = q._record_context(source)
        self.assertEqual(context.factory, '蓝色河谷')
        self.assertEqual(context.destination, '广东省惠州惠阳区')
        self.assertEqual(context.phone.suffix, '7654')
        self.assertEqual(repr(context).count('惠州'), 0)
        self.assertIsNone(q._record_context(source, allow_destination=False).destination)


class RegionTests(unittest.TestCase):
    def test_explicit_region_without_city_suffix_preserves_text_and_discards_contacts(self):
        samples = {
            '安徽省宣城绩溪县合成镇合成街100号 收件人合成人 13900007654': '安徽省宣城绩溪县',
            '安徽省宣城绩溪县合成超市合成镇100号': '安徽省宣城绩溪县',
            '收货地址：广东省惠州惠阳区合成镇88号，合成人 13900007654': '广东省惠州惠阳区',
            '地址: 广东省东莞市长安镇合成路88号 联系人合成人': '广东省东莞市长安镇',
            '广东省中山市小榄镇合成路1号': '广东省中山市小榄镇',
            '广东省东莞市南城街道合成路1号': '广东省东莞市南城街道',
            '北京市海淀区合成园1号': '北京市海淀区',
            '广西壮族自治区南宁市青秀区合成路1号': '广西壮族自治区南宁市青秀区',
            '新疆维吾尔自治区乌鲁木齐市天山区合成路1号': '新疆维吾尔自治区乌鲁木齐市天山区',
            '内蒙古自治区呼和浩特市赛罕区合成路1号': '内蒙古自治区呼和浩特市赛罕区',
            '浙江省宁波市镇海区合成街道99号': '浙江省宁波市镇海区',
            '江苏省镇江市京口区合成路': '江苏省镇江市京口区',
            '江西省景德镇市昌江区合成路': '江西省景德镇市昌江区',
            '河南省南阳市镇平县合成路': '河南省南阳市镇平县',
            '广东省中山市南朗街道某路30号厂房3卡/某公司 联系人合成人': '广东省中山市南朗街道',
            '广东省中山市南朗街道某路30号\n联系人合成人（备用）/白班': '广东省中山市南朗街道',
            '广东省深圳市南山区合成路; 联系人合成人': '广东省深圳市南山区',
            '深圳南山区合成园1号': '深圳南山区',
        }
        for source, expected in samples.items():
            with self.subTest(expected=expected):
                self.assertEqual(q._mes_destination_region(source), expected)

    def test_ambiguous_multiline_contact_first_and_unbounded_regions_are_not_guessed(self):
        for value in (None, 3, '', '广东省深圳市', '广东省深圳市南山区\n北京市海淀区',
                      '广东省深圳市南山区;北京市海淀区', '广东省深圳市南山区 或 上海市浦东新区',
                      '收件人合成人 广东省深圳市南山区', '电话13900007654广东省深圳市南山区',
                      '广东省深圳市南山区/福田区', '<地址>广东省深圳市南山区', '字' * 2001):
            with self.subTest(type=type(value).__name__):
                self.assertIsNone(q._mes_destination_region(value))

    def test_supplier_parsed_addresses_allow_normalization_but_reject_explicit_conflicts(self):
        self.assertEqual(q._eta_route_relation('广东省深圳市南山区', '广东省深圳市南山区'), 'EXACT')
        self.assertEqual(q._eta_route_relation('广东省深圳市南山区', '广东,深圳市,南山区'), 'NORMALIZED_COMPATIBLE')
        self.assertEqual(q._eta_route_relation('广东省深圳市南山区', '广东， 深圳市，福田区'), 'EXPLICIT_CONFLICT')
        self.assertEqual(q._eta_route_relation('安徽省宣城绩溪县', '安徽省宣城市绩溪县'), 'NORMALIZED_COMPATIBLE')
        self.assertEqual(q._eta_route_relation('深圳南山区合成园', '广东省深圳市南山区'), 'NORMALIZED_COMPATIBLE')
        self.assertEqual(q._eta_route_relation('广东省深圳市南山区', '广东省深圳市福田区'), 'EXPLICIT_CONFLICT')
        self.assertEqual(q._eta_route_relation('广东省深圳市南山区', '广东省东莞市长安镇'), 'EXPLICIT_CONFLICT')
        self.assertEqual(q._eta_route_relation('安徽省宣城绩溪县', '浙江省杭州市西湖区'), 'EXPLICIT_CONFLICT')
        for empty in (None, '', ' ', 3, '广东省\n深圳市', '<地址>'):
            self.assertEqual(q._eta_route_relation('广东省深圳市南山区', empty), 'MISSING_OR_INVALID')


class ContextIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_six_exact_factories_choose_authorized_source_once_per_record(self):
        factories = ['瑞欣特', '埃斯蒙特', '硕特科技', '株洲工厂', '蓝色河谷', '东莞工厂', '未知工厂', '瑞欣特分公司', '硕特']
        for i, factory in enumerate(factories):
            for prefix in ('SF', 'KY', 'DPK'):
                server = PhoneServer()
                server.mes_rows = [{'物流单号': prefix + str(i + 1).zfill(12), '出货工厂': factory,
                                    '发货通知人手机号码': '13900007654'}]
                result = await query(server, factory_phone_policy=policy())
                calls = [c for c in server.calls if c['name'] == 'query_trace']
                self.assertEqual(len(calls), 1)
                actual = calls[0]['arguments']['phone']
                expected = ('9187' if factory == '瑞欣特' else '7654') if prefix == 'SF' else (
                    '13900008276' if factory == '硕特科技' and prefix == 'KY' else '')
                self.assertEqual(actual, expected)
                self.assertEqual(server.mes_calls, 1)
                self.assertEqual(result['rows'][0]['tracking_status'], 'DELIVERED')
                serialized = json.dumps(result, ensure_ascii=False)
                for secret in ('13900007654', '13900008276', '13900009187', '7654', '9187', '出货工厂'):
                    self.assertNotIn(secret, serialized)

    async def test_factory_trim_is_allowed_but_similar_names_keep_original_mes_behavior(self):
        for factory, expected in [(' 瑞欣特 ', '9187'), ('瑞欣特有限公司', '7654'), ('瑞 欣特', '7654')]:
            server = PhoneServer()
            server.mes_rows = [{'物流单号': 'SF927365110001', '出货工厂': factory, '发货通知人手机号码': '13900007654'}]
            await query(server, factory_phone_policy=policy())
            self.assertEqual(server.calls[0]['arguments']['phone'], expected)

    async def test_explicit_override_and_record_phone_never_bleed_across_records(self):
        server = PhoneServer()
        server.mes_rows = [
            {'物流单号': 'SF927365110001', '出货工厂': '瑞欣特', '发货通知人手机号码': ''},
            {'物流单号': 'SF920749420002', '出货工厂': '埃斯蒙特', '发货通知人手机号码': ''},
            {'物流单号': 'KY998385030003', '出货工厂': '硕特科技', '发货通知人手机号码': ''},
            {'物流单号': 'KY941254160004', '出货工厂': '蓝色河谷', '发货通知人手机号码': ''},
        ]
        result = await query(server, factory_phone_policy=policy())
        args = [c['arguments'] for c in server.calls]
        self.assertEqual([(a['kuaidiNum'], a['phone']) for a in args], [
            ('SF927365110001', '9187'), ('KY998385030003', '13900008276'), ('KY941254160004', '')])
        self.assertEqual(result['rows'][1]['tracking_status'], 'PHONE_REQUIRED')
        self.assertEqual(server.mes_calls, 1)

    async def test_without_enabled_policy_original_phone_behavior_remains(self):
        server = PhoneServer()
        server.mes_rows = [{'物流单号': 'SF927365110001', '出货工厂': '瑞欣特', '发货通知人手机号码': '13900007654'}]
        await query(server)
        self.assertEqual(server.calls[0]['arguments']['phone'], '7654')

    async def test_private_full_phone_echo_cannot_leak_to_results_or_forecast_history(self):
        server = Server()
        server.trace.update(kuaidiCom='kuayue', fromTo='广东省深圳市南山区 -> 安徽省宣城市绩溪县')
        server.trace['data'][0]['context'] = '合成运输动态 校验13900008276 或139-0000-8276；快递员13900007654'
        server.mes_rows = [{'物流单号': 'KY927365110001', '出货工厂': '硕特科技'}]
        result = await query(server, factory_phone_policy=policy())
        self.assertEqual(server.calls[0]['arguments']['phone'], '13900008276')
        self.assertEqual(result['rows'][0]['eta_status'], 'AVAILABLE')
        for serialized in (json.dumps(result, ensure_ascii=False), json.dumps(server.calls[1], ensure_ascii=False)):
            self.assertNotIn('13900008276', serialized)
            self.assertNotIn('139-0000-8276', serialized)
            self.assertIn('13900007654', serialized)

    def test_four_digit_suffix_never_redacts_unrelated_numbers(self):
        self.assertEqual(q._private_phone_text('合成记录7654，快递员13900007654', '7654'),
                         '合成记录7654，快递员13900007654')

    async def test_mes_region_completes_only_matching_record_missing_destination(self):
        server = Server(); server.trace['fromTo'] = '广东省深圳市南山区 -> '
        server.mes_rows = [{'物流单号': WAYBILL, '收货地址': '安徽省宣城绩溪县合成镇合成街100号 收件人合成人 电话13900007654',
                            '出货工厂': '蓝色河谷'}]
        server.result_patch['toName'] = '安徽省宣城市绩溪县'
        result = await query(server)
        self.assertEqual(result['counts']['eta_requests'], 1)
        self.assertEqual(server.calls[-1]['arguments']['to'], '安徽省宣城绩溪县')
        self.assertEqual(result['rows'][0]['eta_status'], 'AVAILABLE')
        for private in ('13900007654', '合成镇', '合成人', '收货地址', '绩溪县', '蓝色河谷'):
            self.assertNotIn(private, json.dumps(result, ensure_ascii=False))
            if private in ('13900007654', '合成镇', '合成人'):
                self.assertNotIn(private, json.dumps(server.calls, ensure_ascii=False))

    async def test_multiple_records_keep_destination_pairing_and_shared_text_is_not_assumed(self):
        server = Server(); server.trace['fromTo'] = '广东省深圳市南山区 -> '
        server.mes_rows = [
            {'物流单号': 'DPK927365110001', '收货地址': '安徽省宣城绩溪县合成镇1号'},
            {'物流单号': 'DPK920749420002', '收货地址': ''},
            {'物流单号': 'DPK998385030003,DPK941254160004', '收货地址': '广东省惠州惠阳区合成镇2号'},
        ]
        result = await query(server)
        self.assertEqual(result['counts']['tracking_requests'], 4)
        self.assertEqual(result['counts']['eta_requests'], 1)
        self.assertEqual([c['arguments']['to'] for c in server.calls if c['name'] == 'estimate_time_with_logistic'], ['安徽省宣城绩溪县'])
        self.assertTrue(all(not row.get('estimated_arrival_time') for row in result['rows'][1:]))

    async def test_existing_trace_endpoint_is_kept_and_explicit_conflict_suppresses_forecast(self):
        for destination, expected_count in [('广东省深圳市南山区', 1), ('广东省深圳市福田区', 0)]:
            server = Server(); server.trace['fromTo'] = '安徽省宣城市绩溪县 -> 广东省深圳市南山区'
            server.mes_rows = [{'物流单号': WAYBILL, '收货地址': destination + '合成街99号'}]
            result = await query(server)
            self.assertEqual(result['counts']['eta_requests'], expected_count)
            if expected_count:
                self.assertEqual(server.calls[-1]['arguments']['to'], '广东省深圳市南山区')
            else:
                self.assertNotIn('预计送达', result['display_markdown'])

    async def test_forecast_identity_fields_are_checked_if_supplier_returns_them(self):
        for patch in ({'kuaidiNum': 'WRONG0000001'}, {'kuaidicom': 'kuayue'}):
            server = Server(); server.result_patch.update(patch)
            result = await query(server)
            self.assertEqual(result['rows'][0]['eta_status'], 'UNVERIFIED')
            self.assertNotIn('预计送达', result['display_markdown'])


if __name__ == '__main__':
    unittest.main()
