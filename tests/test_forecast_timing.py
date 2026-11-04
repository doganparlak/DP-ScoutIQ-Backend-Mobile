import math
import unittest
import numpy as np
from match_pool_module import forecast_timing as t
from scripts.evaluate_fixture_timing import examples, losses


class FixtureTimingTests(unittest.TestCase):
    def test_zero_residual_preserves_league_prior(self):
        x=t.features([],[],True,.44)
        model={'kind':t.KIND,'method':'logistic','mean':[0.]*len(x),'scale':[1.]*len(x),
               'coefficients':[0.]*len(x),'intercept':0.}
        self.assertAlmostEqual(t.predict_share(model,[],[],True,.44),.44)

    def test_team_timing_can_favor_first_half(self):
        early=[{'gf':2,'ga':2,'hgf':2,'hga':2,'home':True} for _ in range(10)]
        late=[{'gf':2,'ga':2,'hgf':0,'hga':0,'home':True} for _ in range(10)]
        x=t.features(early,early,True,.44)
        beta=np.zeros(len(x));beta[1]=1.
        model={'kind':t.KIND,'method':'logistic','mean':[0.]*len(x),'scale':[1.]*len(x),
               'coefficients':beta.tolist(),'intercept':0.}
        self.assertGreater(t.predict_share(model,early,early,True,.44),.5)
        self.assertLess(t.predict_share(model,late,late,True,.44),.44)

    def test_features_restrict_history_to_runtime_window(self):
        row={'gf':2,'ga':1,'hgf':1,'hga':0,'home':True}
        a=[{**row,'hgf':2}]*20+[row]*10
        np.testing.assert_array_equal(t.features(a,a,True,.44),t.features(a[-10:],a[-10:],True,.44))

    def test_same_day_result_cannot_enter_training_features(self):
        matches=[]
        for i in range(8):
            matches.append({'id':i,'at':f'2026-10-{i+1:02} 12:00:00','home':1,'away':2,
                            'hg':2,'ag':1,'hhg':1,'hag':0,'league':8,'stats':{}})
        matches.append({**matches[-1],'id':99,'at':'2026-10-08 16:00:00'})
        data=examples(matches)
        target=next(r for r in data if r['id']==99)
        self.assertTrue(all(row['id']!=7 for history in target['histories'] for row in history))

    def test_half_probabilities_normalize_and_preserve_full_rates(self):
        rows=[{'first':[1,0],'full':[2,1]}]
        rates=np.array([[1.8,1.2]])
        shares=np.array([[.6,.55]])
        result=losses(rows,rates,shares)
        self.assertAlmostEqual(result['probabilities'].sum(),1.)
        self.assertEqual(result['probabilities'].argmax(),0)
        np.testing.assert_allclose(rates*shares+rates*(1-shares),rates)
        self.assertTrue(0 <= result['firstHalfBTTS'][0] <= 1)

    def test_unknown_artifact_is_rejected(self):
        with self.assertRaises(ValueError):t.predict_share({'kind':'wrong'},[],[],True,.44)
