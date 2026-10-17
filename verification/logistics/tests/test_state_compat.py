"""Public Kuaidi100 state vocabulary; synthetic records, no external requests."""
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest

import httpx

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType('r52_state_tests')
package.__path__ = [str(ROOT / 'backend/app')]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location(package.__name__ + '.logistics_query', ROOT / 'backend/app/logistics_query.py')
q = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = q
spec.loader.exec_module(q)
WAYBILL = 'DPK927365110001'
ORDER = 'SDXM9307330001'


def payload(state, status=None, *, status_code=None, context='合成动态'):
    event = {'time': '2026-10-09 12:00:00', 'status': state if status is None else status, 'context': context}
    if status_code is not None:
        event['statusCode'] = status_code
    return {'kuaidiNum': WAYBILL, 'kuaidiCom': 'debangkuaidi', 'kuaidiName': '合成承运商',
            'state': state, 'fromTo': '合成起点 -> 合成终点', 'data': [event]}


def apply(body):
    row = q._row(ORDER, WAYBILL, 'MES_FOUND', waybill=WAYBILL, carrier='德邦')
    q._apply_mcp_trace(row, {'structuredContent': body})
    return row


def rendered(row):
    return q.render_logistics_table({'rows': [row], 'truncation': {}}, [ORDER])


class StateContract(unittest.TestCase):
    def test_official_signed_labels_and_codes_produce_verified_signature(self):
        labels = [('3', '签收'), ('301', '本人签收'), ('302', '派件异常后签收'),
                  ('303', '代签'), ('304', '投柜或站签收')]
        for code, label in labels:
            for top in (code, label):
                for event_status in (code, label):
                    with self.subTest(top=top, event_status=event_status):
                        row = apply(payload(top, event_status))
                        self.assertEqual(row['tracking_status'], 'DELIVERED')
                        self.assertEqual(row['signed_event_time'], '2026-10-09 12:00:00')
                        self.assertIn('已签收&#10;&#10;签收时间：2026-10-09 12:00', rendered(row))

    def test_documented_station_signed_alias(self):
        row = apply(payload('投柜或驿站签收'))
        self.assertEqual(row['tracking_status'], 'DELIVERED')
        self.assertEqual(row['signed_event_time'], '2026-10-09 12:00:00')

    def test_explicit_event_code_can_identify_status_omitted_from_label(self):
        for code in ('301', 302, '303', 304):
            row = apply(payload('已签收', '', status_code=code))
            self.assertEqual(row['signed_event_time'], '2026-10-09 12:00:00')

    def test_locker_501_is_not_signed_and_does_not_use_eta(self):
        for state in ('501', '投柜或驿站'):
            body = payload(state, context='此自由文本写了已签收，也不能替代结构化状态')
            body['arrivalTime'] = '2026-10-10 18'
            row = apply(body)
            self.assertEqual(row['tracking_status'], 'AT_PICKUP_POINT')
            self.assertNotIn('signed_event_time', row)
            self.assertNotIn('estimated_arrival_time', row)
            self.assertIn('待取件&#10;&#10;动态时间：', rendered(row))

    def test_not_signed_exception_and_unknown_prose_never_match_substrings(self):
        for state in ('201', '超时未签收', '205', '柜或驿站超时未取', '未签收', '疑似已签收',
                      '签收失败', '签收/派件异常', '签收后请访问 https://example.invalid'):
            row = apply(payload(state))
            self.assertNotEqual(row['tracking_status'], 'DELIVERED')
            self.assertNotIn('signed_event_time', row)

    def test_return_refusal_and_pending_refusal_are_distinct(self):
        for state, expected, label in [('4', 'RETURN_SIGNED', '退签'), ('退签', 'RETURN_SIGNED', '退签'),
                                       ('14', 'REFUSED', '拒签'), ('拒签', 'REFUSED', '拒签'),
                                       ('6', 'RETURNING', '退回中'), ('退回', 'RETURNING', '退回中'),
                                       ('203', 'REJECTION_REQUESTED', '拒收待确认'),
                                       ('拒收', 'REJECTION_REQUESTED', '拒收待确认')]:
            row = apply(payload(state))
            self.assertEqual(row['tracking_status'], expected)
            self.assertIn(label + '&#10;&#10;动态时间：', rendered(row))
            self.assertNotIn('signed_event_time', row)

    def test_advanced_in_transit_states_allow_actual_source_eta(self):
        for state in ('1001', '到达派件城市', '1002', '干线', '1003', '转递'):
            body = payload(state)
            body['arrivalTime'] = '2026-10-10 18'
            row = apply(body)
            self.assertEqual(row['tracking_status'], 'IN_TRANSIT')
            self.assertEqual(row['estimated_arrival_time'], '2026-10-10 18')
            self.assertIn('预计送达：2026-10-10 18时', rendered(row))

    def test_pre_collection_transfer_and_customs_remain_specific(self):
        cases = [('101', '已下单'), ('102', '待揽收'), ('103', '已揽收'), ('7', '转投'),
                 ('401', '已销单'), ('10', '待清关'), ('11', '清关中'), ('12', '已清关'), ('13', '清关异常')]
        for code, label in cases:
            row = apply(payload(code))
            self.assertIn(label + '&#10;&#10;', rendered(row))
            self.assertNotIn('signed_event_time', row)

    def test_known_label_code_conflicts_cannot_manufacture_signature(self):
        for label, code in [('超时未签收', '301'), ('本人签收', '201'),
                            ('投柜或驿站', '304'), ('派件', '3')]:
            row = apply(payload('已签收', label, status_code=code))
            self.assertNotIn('signed_event_time', row)
            self.assertIn('签收时间：暂未提供', rendered(row))

    def test_invalid_code_types_do_not_coerce_to_valid_codes(self):
        for code in (True, False, 3.0, 301.0, -3, '003', '3e0', '３０１', {}, [], '3x'):
            row = apply(payload('已签收', '未知状态', status_code=code))
            self.assertNotIn('signed_event_time', row)

    def test_trim_outer_whitespace_without_guessing_labels(self):
        row = apply(payload(' 本人签收 ', ' 301 '))
        self.assertEqual(row['tracking_status'], 'DELIVERED')
        self.assertIn('signed_event_time', row)
        self.assertEqual(apply(payload('本人 签收'))['tracking_status'], 'TRACKING_AVAILABLE')

    def test_unknown_top_level_not_inferred_from_event_or_free_text(self):
        row = apply(payload('上游尚无可确认状态', '本人签收', context='签收文案仅是数据'))
        self.assertEqual(row['tracking_status'], 'TRACKING_AVAILABLE')
        self.assertNotIn('signed_event_time', row)
        row = apply(payload('在途', '在途', context='客服称已签收，2099-12-31'))
        self.assertEqual(row['tracking_status'], 'IN_TRANSIT')
        self.assertNotIn('signed_event_time', row)

    def test_sign_time_uses_latest_explicit_signed_event_not_latest_non_sign_update(self):
        body = payload('已签收', '在途')
        body['data'].extend([
            {'time': '2026-10-09T02:30:00Z', 'status': '代签', 'context': '合成早签收'},
            {'time': '2026-10-09T11:00:00+08:00', 'status': '301', 'context': '合成签收'},
        ])
        row = apply(body)
        self.assertEqual(row['signed_event_time'], '2026-10-09T11:00:00+08:00')
        self.assertEqual(row['event_time'], '2026-10-09 12:00:00')
        self.assertIn('签收时间：2026-10-09 11:00', rendered(row))

    def test_unparseable_timestamp_does_not_become_signed_time(self):
        body = payload('301')
        body['data'][0]['time'] = 'today'
        row = apply(body)
        self.assertTrue(row['tracking_time_unverified'])
        self.assertNotIn('signed_event_time', row)


class RealtimeContract(unittest.IsolatedAsyncioTestCase):
    async def test_same_official_state_mapping_for_customer_query_api(self):
        for state, expected in [('301', 'DELIVERED'), (302, 'DELIVERED'), ('501', 'AT_PICKUP_POINT'),
                                ('201', 'EXCEPTION'), ('4', 'RETURN_SIGNED'), ('14', 'REFUSED')]:
            def handler(request):
                if str(request.url) == q.MES_ENDPOINT:
                    return httpx.Response(200, json={'code': 0, 'ObjectData': [{'物流单号': WAYBILL}]})
                return httpx.Response(200, json={'status': '200', 'com': 'debangkuaidi', 'nu': WAYBILL,
                    'state': state, 'data': [{'ftime': '2026-10-09 12:00:00', 'status': '',
                                            'statusCode': str(state), 'context': '合成动态'}]})
            config = q.LogisticsConfig(enabled=True, kuaidi100=q.Kuaidi100Config(
                'SYNTHETIC_CUSTOMER', 'SYNTHETIC_KEY', carrier_codes={'德邦': 'debangkuaidi'}, single_process_rate_guard=True))
            service = q.LogisticsQueryService(config, transport=httpx.MockTransport(handler))
            result = await service.query({'query_text': ORDER}, trusted_user_no='SYNTHETIC', authorize=lambda: True)
            self.assertEqual(result['rows'][0]['tracking_status'], expected)
            self.assertEqual('signed_event_time' in result['rows'][0], expected == 'DELIVERED')


if __name__ == '__main__':
    unittest.main()
