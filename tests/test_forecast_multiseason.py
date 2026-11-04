import unittest
from datetime import datetime, timezone, timedelta
import numpy as np
from match_pool_module import forecast as f, forecast_timing as timing
from match_pool_module.forecast_multiseason import SeasonHistory, make_examples, weights
from scripts.evaluate_multiseason_forecasts import main_metrics, timing_metrics, paired


def fixture(identity,date,season=1):
    return {'id':identity,'at':date,'season':season,'league':8,'home':1,'away':2,
            'hg':2,'ag':1,'hhg':1,'hag':0,'stats':{}}


class MultiSeasonValidationTests(unittest.TestCase):
    def test_new_season_clears_league_totals_without_consuming_result(self):
        state=SeasonHistory();state.consume(fixture(1,'2025-05-01 12:00:00'))
        self.assertEqual(state.totals[8][0],1)
        state.prepare(fixture(2,'2025-08-01 12:00:00',2))
        self.assertNotIn(8,state.totals);self.assertNotIn(8,state.half_totals)
        self.assertNotIn(2,state.seen)

    def test_offseason_history_is_not_carried_into_recent_form(self):
        state=SeasonHistory();state.consume(fixture(1,'2025-01-01 12:00:00'))
        state.prepare(fixture(2,'2025-08-01 12:00:00',2))
        self.assertEqual(state.histories[1],[])

    def test_recency_weights_prioritize_recent_labels(self):
        data={'leagues':[8,8],'times':['2024-10-01 12:00:00','2025-10-01 12:00:00']}
        w=weights(data,[0,1],f.utc('2026-01-01'),180)
        self.assertGreater(w[2],w[0]);self.assertEqual(w[0],w[1])
        with self.assertRaises(ValueError):weights(data,[0,1],f.utc('2024-01-01'),180)

    def test_half_time_predictions_preserve_normalization_and_missing_labels(self):
        result=timing_metrics(np.array([[2.,1.],[1.,1.]]),np.array([[1.,0.],[np.nan,np.nan]]),
                              np.array([[1.7,1.1],[1.,1.]]),np.array([[.45,.43],[.6,.6]]))
        self.assertEqual(len(result['halfAccuracy']),1)
        self.assertAlmostEqual(float(result['halfProbabilities'].sum()),1.)

    def test_score_metrics_match_serving_grid(self):
        goals=np.array([[2.,1.],[0.,0.]])
        rates=np.array([[1.8,1.1],[.4,.4]])
        result=main_metrics(goals,rates)
        for i,(h,a)in enumerate(rates):
            d=f.score_distribution(h,a)
            np.testing.assert_allclose(result['outcomeProbabilities'][i],[d['outcomes'][k]for k in ('home','draw','away')],rtol=1e-10,atol=1e-10)
            self.assertEqual(bool(result['exactScoreAccuracy'][i]),d['topScores'][0]['home']==goals[i,0]and d['topScores'][0]['away']==goals[i,1])

    def test_batch_timing_matches_portable_runtime(self):
        history=[{'gf':2,'ga':1,'hgf':1,'hga':0,'home':True}for _ in range(10)]
        x=timing.features(history,history,True,.44)
        model={'kind':timing.KIND,'method':'logistic','intercept':.1,'mean':[0.]*len(x),'scale':[1.]*len(x),
               'coefficients':[.02]*len(x),'blend':.5}
        self.assertAlmostEqual(timing.predict_share(model,history,history,True,.44),timing.predict_matrix(model,[x],[.44])[0])

    def test_identical_predictions_have_zero_paired_difference(self):
        r=paired(np.zeros(4),['2026-10-11','2026-10-11','2026-10-12','2026-10-13'])
        self.assertEqual(r['dateCluster95CI'],[0.,0.]);self.assertEqual(r['matchdays'],3)

    def test_newest_result_cannot_enter_earlier_features(self):
        matches=[fixture(i,f'2026-09-{i+1:02} 12:00:00')for i in range(10)]
        prior={'coefficients':{'England':100.},'availableAt':'2025-06-01','source':'test'}
        comps=[{'leagueId':8,'country':'England'}]
        a=make_examples(matches,'legacy',comps,prior)
        b=make_examples(matches+[fixture(99,'2026-09-30 12:00:00')],'legacy',comps,prior)
        np.testing.assert_array_equal(a['raw'],b['raw'][:len(a['raw'])])
        self.assertEqual(a['ids'],b['ids'][:-1])
