"""Model switching must not charge existing unlocks or change past forecasts."""
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timedelta, timezone
from mobile_pro_module import match_forecast as access
from match_pool_module.forecast_league_offsets import fixture_elo_gap, adjust_elo_context

class ActivationTests(unittest.TestCase):
    def payload(self, days, model=9):
        return {'status':'ready','forecast':{'startingAt':(datetime.now(timezone.utc)+timedelta(days=days)).isoformat(),'modelId':model}}

    def test_unlocked_future_refresh_does_not_spend_credit(self):
        saved, fresh = self.payload(1), self.payload(1,10)
        db=Mock()
        with patch.object(access,'_schema'), patch.object(access,'_account',return_value={'unlimited':False,'free_chat_messages_remaining':0}), patch.object(access,'_opened',return_value=saved), patch.object(access,'active_model_id',return_value=10), patch.object(access,'get_forecast',return_value=fresh):
            result=access.forecast_access(db,123,456,unlock=True)
        self.assertTrue(result['unlocked'])
        self.assertEqual(result['forecast']['modelId'],10)
        self.assertEqual(result['freeChatMessagesRemaining'],0)
        self.assertEqual(db.execute.call_count,1)
        self.assertIn('UPDATE mobile_pro_forecast_unlocks',str(db.execute.call_args.args[0]))
        db.commit.assert_called_once()

    def test_unlocked_legacy_format_refreshes_on_same_model_without_credit(self):
        saved=self.payload(1,10)
        saved['forecast'].update(scoreMargins={'draw':.3},halfScenarios={'firstHalfBothTeamsScore':.2})
        fresh=self.payload(1,10)
        fresh['forecast'].update(scoreMargins={'draw':.3},halfScenarios={'projectedGoals':{'first':{'home':.5,'away':.4},'second':{'home':.7,'away':.6}}})
        db=Mock()
        with patch.object(access,'_schema'),patch.object(access,'_account',return_value={'unlimited':False,'free_chat_messages_remaining':0}),patch.object(access,'_opened',return_value=saved),patch.object(access,'active_model_id',return_value=10),patch.object(access,'get_forecast',return_value=fresh) as fetch:
            result=access.forecast_access(db,123,456)
        fetch.assert_called_once_with(456)
        self.assertEqual(result['forecast'],fresh['forecast'])
        self.assertTrue(result['unlocked'])
        self.assertEqual(result['freeChatMessagesRemaining'],0)
        self.assertEqual(db.execute.call_count,1)
        self.assertIn('UPDATE mobile_pro_forecast_unlocks',str(db.execute.call_args.args[0]))

    def test_current_format_keeps_existing_unlock(self):
        saved=self.payload(1,10)
        saved['forecast'].update(scoreMargins={'draw':.3},halfScenarios={'projectedGoals':{'first':{},'second':{}}})
        db=Mock()
        with patch.object(access,'active_model_id',return_value=10),patch.object(access,'get_forecast') as fetch:
            self.assertIs(access._refresh_opened(db,123,456,saved),saved)
        fetch.assert_not_called();db.execute.assert_not_called()

    def test_past_unlock_unchanged(self):
        saved=self.payload(-1);db=Mock()
        with patch.object(access,'get_forecast') as fetch:
            self.assertIs(access._refresh_opened(db,1,2,saved),saved)
        fetch.assert_not_called();db.execute.assert_not_called()

    def test_processing_fresh_forecast_does_not_replace_paid_payload(self):
        db=Mock()
        with patch.object(access,'active_model_id',return_value=10),patch.object(access,'get_forecast',return_value={'status':'processing'}):
            self.assertEqual(access._refresh_opened(db,1,2,self.payload(1)),{'status':'processing'})
        db.execute.assert_not_called()

    def test_serving_offset_scope_and_pair_center(self):
        model={'leagueOffsets':{'kind':'residual_league_elo_offset_v1','internationalCompetitionIds':[2,5,2286],'teamAssociations':{'1':'England','2':'Türkiye'},'offsets':{'England':100.,'Türkiye':-30.}}}
        fixture={'league':{'id':2},'homeTeam':{'id':1},'awayTeam':{'id':2}}
        self.assertEqual(fixture_elo_gap(model,fixture),130.)
        fixture['league']['id']=8
        self.assertEqual(fixture_elo_gap(model,fixture),0.)
        fixture['league']['id']=2;fixture['awayTeam']['id']=3
        self.assertEqual(fixture_elo_gap(model,fixture),0.)
        context=adjust_elo_context({'rating':1500.,'opponent_rating':1600.},130.,True)
        self.assertEqual(context,{'rating':1565.,'opponent_rating':1535.})
