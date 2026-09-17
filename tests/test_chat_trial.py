import hashlib
import json
import unittest
from unittest.mock import MagicMock

from fastapi import HTTPException
from api_module.chat_trial import reserve_message, refund_message


class ChatTrialTests(unittest.TestCase):
    def db_for(self, plan, credits, existing=None):
        db = MagicMock()
        user_result, replay_result = MagicMock(), MagicMock()
        user_result.mappings.return_value.first.return_value = {
            'plan': plan, 'free_chat_messages_remaining': credits,
        }
        replay_result.mappings.return_value.first.return_value = existing
        db.execute.side_effect = [user_result, replay_result, MagicMock(), MagicMock()]
        return db

    def reserve(self, db):
        return reserve_message(db, 7, 'request-1', 'player', 'session-1', '')

    def test_free_and_plus_reserve_one_credit(self):
        for plan in ('Free', 'No Ads Monthly'):
            with self.subTest(plan=plan):
                db = self.db_for(plan, 5)
                self.assertIsNone(self.reserve(db))
                sql = [str(call.args[0]) for call in db.execute.call_args_list]
                self.assertEqual(sum('free_chat_messages_remaining - 1' in q for q in sql), 1)
                self.assertTrue(any('INSERT INTO free_chat_requests' in q for q in sql))
                db.commit.assert_called_once()

    def test_zero_credits_reject_without_debit(self):
        for plan in ('Free', 'No Ads Monthly'):
            with self.subTest(plan=plan):
                db = self.db_for(plan, 0)
                with self.assertRaises(HTTPException) as error:
                    self.reserve(db)
                self.assertEqual(error.exception.status_code, 403)
                self.assertEqual(db.execute.call_count, 2)
                db.commit.assert_not_called()

    def test_replay_after_plus_upgrade_does_not_charge_again(self):
        fingerprint = hashlib.sha256(json.dumps(['player', 'session-1', ''], ensure_ascii=False).encode()).hexdigest()
        db = self.db_for('No Ads Monthly', 4, {
            'request_hash': fingerprint, 'response_json': {'response': 'cached'},
        })
        self.assertEqual(self.reserve(db), {'response': 'cached', 'freeChatMessagesRemaining': 4})
        self.assertEqual(db.execute.call_count, 2)
        db.commit.assert_not_called()

    def test_failed_request_refunds_once(self):
        db = MagicMock()
        removed, absent = MagicMock(), MagicMock()
        removed.first.return_value = (7,)
        absent.first.return_value = None
        db.execute.side_effect = [removed, MagicMock(), absent]
        refund_message(db, 7, 'request-1')
        refund_message(db, 7, 'request-1')
        sql = [str(call.args[0]) for call in db.execute.call_args_list]
        self.assertEqual(sum('free_chat_messages_remaining + 1' in q for q in sql), 1)


if __name__ == '__main__':
    unittest.main()
