import unittest
import numpy as np
from scripts.evaluate_half_allocation import allocate,groupmass,fit_allocation,GROUP
from scripts.evaluate_multiseason_forecasts import timing_metrics
from scipy.special import softmax

class HalfAllocationTests(unittest.TestCase):
 def test_zero_tilt_matches_current_poisson_splitting(self):
  rates=np.array([[1.5,.9],[.5,.6],[3.,2.5]]);shares=np.array([[.44,.46],[.6,.55],[.4,.47]])
  p,b,first,check=allocate(np.ones((3,1)),np.zeros((1,3)),rates,shares)
  expected=timing_metrics(np.ones((3,2)),np.zeros((3,2)),rates,shares)
  np.testing.assert_allclose(p,expected['halfProbabilities'],atol=2e-11)
  np.testing.assert_allclose(first,rates*shares,atol=2e-10)
  np.testing.assert_allclose(b,(1-np.exp(-rates[:,0]*shares[:,0]))*(1-np.exp(-rates[:,1]*shares[:,1])),atol=2e-11)
  self.assertLess(check['omittedGridMassMax'],1e-10)
 def test_tilt_preserves_every_full_time_score_cell(self):
  shares=np.array([[.2,.7],[.55,.6]]);factor=np.exp(np.array([[3.,-2.,1.],[-1.,4.,-2.]])@GROUP.T)
  for h in range(7):
   for a in range(7):
    base=groupmass(h,a,shares);q=base*factor;q/=q.sum(axis=1)[:,None]
    np.testing.assert_allclose(q.sum(axis=1),1,atol=1e-14)
    self.assertTrue(np.all(q[base==0]==0))
    if (h+a)%2:self.assertTrue(np.all(q[:,2:4]==0))
    if h==0 or a==0:self.assertTrue(np.all(q[:,1::2]==0))
 def test_likelihood_learns_first_half_signal(self):
  n=120;x=np.column_stack([np.ones(n),np.r_[np.ones(n//2),-np.ones(n//2)]]);base=groupmass(2,1,np.full((n,2),.44));y=np.r_[np.zeros(n//2,dtype=int),np.full(n//2,4)]
  w,status=fit_allocation(x,base,y,np.ones(n),10.)
  p=softmax(np.log(np.maximum(base,1e-300))+(x@w)@GROUP.T,axis=1)
  self.assertGreater(p[:60,:2].sum(axis=1).mean(),p[60:,:2].sum(axis=1).mean())
 def test_event_features_exclude_current_and_same_day_results(self):
  import json,tempfile
  from pathlib import Path
  from unittest.mock import patch
  from scripts import evaluate_half_allocation as module
  with tempfile.TemporaryDirectory()as directory:
   root=Path(directory)
   matches=[{'id':1,'at':'2026-10-01 12:00:00','home':1,'away':2,'hg':1,'ag':0,'hhg':1,'hag':0}, {'id':2,'at':'2026-10-02 12:00:00','home':1,'away':2,'hg':1,'ag':0,'hhg':1,'hag':0}, {'id':3,'at':'2026-10-02 19:00:00','home':1,'away':2,'hg':1,'ag':0,'hhg':1,'hag':0}]
   periods={str(m['id']):{'periods':2,'stats':{},'events':[{'half':1,'minute':12,'result':'1-0','rescinded':False}]}for m in matches}
   (root/'dataset.json').write_text(json.dumps({'matches':matches}));(root/'period-data.json').write_text(json.dumps(periods))
   with patch.object(module,'SRC',root),patch.object(module,'OUT',root):
    module.make_features();before=json.loads((root/'event-features.json').read_text())
    matches[1].update(hg=0,hhg=0);periods['2']['events']=[]
    (root/'dataset.json').write_text(json.dumps({'matches':matches}));(root/'period-data.json').write_text(json.dumps(periods));module.make_features();after=json.loads((root/'event-features.json').read_text())
   self.assertEqual(before['2'],after['2']);self.assertEqual(before['3'],after['3']);self.assertTrue(any(before['2']))
