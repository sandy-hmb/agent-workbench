from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import workspace_verification as verification


VALID = '''## 验证批次 2026-09-08T10:00:00Z
- 总体结果：通过
- 审查结论：通过
- 代码状态：{"service":"sha256:AAAAAAAA"}

### 检查 1
- 工作目录：../service
- 命令：./mvnw test
- 退出状态：0
- 结果：测试通过。
'''.replace('AAAAAAAA', 'a' * 64)


class InspectVerificationTest(unittest.TestCase):
    def test_complete_document_has_actual_batch_and_check_source_positions(self):
        text = '# 验证记录\n\n' + VALID
        result = verification.describe_verification_document(text)
        selected = result['selectedBatch']
        self.assertEqual('complete', selected['completeness'])
        self.assertEqual('passed', selected['recordedResult'])
        self.assertEqual('passed', selected['recordedReview'])
        self.assertEqual('2026-09-08T10:00:00Z', selected['recordedAt'])
        self.assertEqual(3, selected['source']['startLine'])
        self.assertEqual(8, selected['checks'][0]['source']['startLine'])
        self.assertEqual('0', selected['checks'][0]['exitStatus'])
        self.assertEqual([], selected['issues'])
        self.assertEqual(result['latestBatchId'], selected['id'])
        self.assertEqual(VALID.strip(), selected['raw'])

    def test_missing_or_duplicate_required_fields_are_not_complete(self):
        for record in [VALID.replace('- 命令：./mvnw test\n', ''),
                       VALID.replace('- 总体结果：通过', '- 总体结果：通过\n- 总体结果：通过'),
                       VALID.replace('- 审查结论：通过\n', ''),
                       VALID.replace('sha256:' + 'a' * 64, 'invalid')]:
            with self.subTest(record=record):
                selected = verification.describe_verification_document(record)['selectedBatch']
                self.assertEqual('incomplete', selected['completeness'])
                self.assertTrue(selected['issues'])

    def test_failed_result_is_independent_of_complete_record(self):
        record = VALID.replace('- 总体结果：通过', '- 总体结果：失败').replace('- 退出状态：0', '- 退出状态：1')
        selected = verification.describe_verification_document(record)['selectedBatch']
        self.assertEqual('failed', selected['recordedResult'])
        self.assertEqual('complete', selected['completeness'])
        self.assertEqual('1', selected['checks'][0]['exitStatus'])

    def test_latest_incomplete_batch_is_preserved_without_fallback(self):
        text = '# 验证\n\n' + VALID + '\n## 验证批次 2026-09-08T11:00:00Z\n- 总体结果：失败\n'
        result = verification.describe_verification_document(text)
        self.assertEqual(2, len(result['batches']))
        self.assertEqual('failed', result['selectedBatch']['recordedResult'])
        self.assertEqual('incomplete', result['selectedBatch']['completeness'])
        self.assertEqual([], result['selectedBatch']['checks'])
        self.assertNotEqual(result['batches'][0]['id'], result['latestBatchId'])

    def test_legacy_and_missing_record_do_not_claim_complete_checks(self):
        self.assertIsNone(verification.describe_verification_document('# 验证\n')['selectedBatch'])
        old = '## 执行记录 2026-09-08\n- 结果：通过\n'
        self.assertEqual('legacy', verification.describe_verification_document(old)['selectedBatch']['completeness'])

    def test_optional_values_only_appear_if_explicitly_recorded(self):
        selected = verification.describe_verification_document(VALID)['selectedBatch']
        self.assertIsNone(selected['checks'][0]['duration'])
        self.assertIsNone(selected['checks'][0]['testCount'])
        changed = VALID.replace('- 结果：测试通过。', '- 结果：测试通过。\n- 耗时：1.2 秒\n- 测试数量：42')
        check = verification.describe_verification_document(changed)['selectedBatch']['checks'][0]
        self.assertEqual('1.2 秒', check['duration'])
        self.assertEqual(42, check['testCount'])


if __name__ == '__main__':
    unittest.main()
