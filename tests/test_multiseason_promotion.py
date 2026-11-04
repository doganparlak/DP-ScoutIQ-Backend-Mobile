"""User-approved champion must survive legacy bot-triggered training jobs."""
import unittest
from unittest.mock import Mock,patch
from match_pool_module import forecast as f


class ChampionPolicyTests(unittest.TestCase):
    def test_weekly_bot_reuses_offline_champion_without_legacy_training(self):
        lock=Mock();lock.execute.side_effect=[Mock(),Mock(scalar=Mock(return_value=None)),Mock(scalar=Mock(return_value=12))]
        manager=Mock();manager.__enter__=Mock(return_value=lock);manager.__exit__=Mock(return_value=False)
        with patch.object(f,'_schema'),patch.object(f.engine,'begin',return_value=manager),patch.object(f,'_request')as provider,patch.object(f,'fit_model')as fit:
            result=f.train_shared_model(refresh_key='score-prediction:123')
        self.assertEqual(result,{'modelId':12,'reused':True,'trainingPolicy':'offline_refit_only'})
        provider.assert_not_called();fit.assert_not_called()

    def test_completed_refresh_key_remains_idempotent(self):
        lock=Mock();lock.execute.side_effect=[Mock(),Mock(scalar=Mock(return_value=12))]
        manager=Mock();manager.__enter__=Mock(return_value=lock);manager.__exit__=Mock(return_value=False)
        with patch.object(f,'_schema'),patch.object(f.engine,'begin',return_value=manager),patch.object(f,'_request')as provider:
            result=f.train_shared_model(refresh_key='score-prediction:123')
        self.assertEqual(result,{'modelId':12,'reused':True});provider.assert_not_called()
