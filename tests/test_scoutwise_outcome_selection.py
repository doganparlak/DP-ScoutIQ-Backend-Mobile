import unittest
from unittest.mock import patch, Mock
from datetime import datetime, timedelta, timezone
from match_pool_module.forecast import score_distribution
from score_prediction_module.scoutwise import outcome_aligned_score, predict_fixtures

class BotOutcomeSelectionTests(unittest.TestCase):
    def example(self):
        draw={'home':1,'away':1,'probability':.15}
        home={'home':2,'away':1,'probability':.12}
        away={'home':1,'away':2,'probability':.08}
        return {'outcomes':{'home':.5,'draw':.3,'away':.2},'topScores':[draw,home,{'home':2,'away':2,'probability':.10}], 'outcomeTopScores':{'home':home,'draw':draw,'away':away}}

    def test_user_example_selects_home_win_instead_of_modal_draw(self):
        f=self.example();self.assertEqual(outcome_aligned_score(f),{'home':2,'away':1,'probability':.12})
        self.assertEqual(f['topScores'][0]['home'],1)

    def test_draw_and_away_leaders(self):
        for winner in ['draw','away']:
            f=self.example();f['outcomes']={key:.6 if key==winner else .2 for key in f['outcomes']}
            self.assertEqual(outcome_aligned_score(f),f['outcomeTopScores'][winner])

    def test_tied_outcomes_use_highest_probability_score_among_leaders(self):
        f=self.example();f['outcomes']={'home':.4,'draw':.4,'away':.2}
        self.assertEqual(outcome_aligned_score(f),f['outcomeTopScores']['draw'])

    def test_full_distribution_can_choose_beyond_visible_matrix(self):
        f=score_distribution(8.,7.)
        best=outcome_aligned_score(f)
        self.assertGreater(best['home'],best['away'])
        self.assertGreater(best['away'],5)
        self.assertGreater(best['probability'],max(s['probability'] for s in f['grid'] if s['home']>s['away']))

    def test_bot_submission_uses_aligned_score_and_preserves_forecast_snapshot(self):
        now=datetime.now(timezone.utc);forecast=self.example();snapshot=Mock()
        fixture={'fixtureId':1,'startingAt':(now+timedelta(days=1)).isoformat(),'state':{'code':'NS'}}
        result={'status':'ready','forecast':forecast}
        with patch('match_pool_module.forecast.build_forecast',return_value=result):
            picks,skipped=predict_fixtures([fixture],{'asOf':(now-timedelta(days=1)).isoformat()},now,snapshot)
        self.assertEqual(picks,{'1':{'home':2,'away':1}});self.assertEqual(skipped,[])
        snapshot.assert_called_once_with(result)
        self.assertEqual(forecast['topScores'][0]['home'],1)
