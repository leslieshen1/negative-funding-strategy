"""Literal synthetic fixtures exercise the real JIT engine, never return evidence."""
from pathlib import Path
import importlib.util,sys,unittest,warnings
import numpy as np
import engine_phase1 as E
from causal_inputs import causal_sigma
from units import paired_token_step

def replay(engine=E,qty=100.,N=2,dry=False,zero_event=False,missing_exit=False,audit=False,positive_payment=False,notice_hour=None,price_jump=False,**extra):
 T=16;a=lambda x:np.full((T,N),x,dtype=float);v=np.ones(N);z=np.zeros(N)
 sv=a(100.);fv=a(99.5);volume=a(1e6)
 if price_jump:sv[2:]*=3.;fv[2:]*=3.
 if dry:volume[1]=0
 ev=a(0.);iv=a(0.)
 if zero_event:iv[2]=8.
 if positive_payment:ev[0]=-.1;ev[2]=.01;iv[[0,2]]=8.
 if missing_exit:
  ev[0]=-.1;ev[2]=.2;ev[3]=-.3;iv[[0,2,3]]=8.
  sv[2]=0;fv[2]=0
 allowed=np.zeros((T,N),np.uint8);allowed[1]=1
 if notice_hour is not None:allowed[3]=1
 used=np.zeros(12);cum=np.zeros(12);ep=np.zeros((100,11));comp=np.zeros((12,6));diag=np.zeros(32)
 pos=np.zeros((12,N));xd=np.zeros(64)
 params=dict(entry_fav_n=.001,entry_basis_n=-.003,capital=1000.,exec_mode=1.,pov=.03,
  impact=0.,fee_s=0.,fee_f=0.,k_imp=0.,derisk=0.,f_exitlag=1.,f_markfund=1.,f_coin=2.,
  f_budget=1.,f_prior=1.,f_hardcap=1.,f_exitpov=1.,xmode=5.,x_obs=1.,conf_h=1.,
  wind_down_h=1.,res_mode=2.,fill_cap=1.,fx_eq=2.,fx_fund=2.)
 params.update(extra)
 if audit:params.update(res_mode=3.,margin_rebalance=1.)
 if engine is not E:params={k:v for k,v in params.items() if k in engine.PI}
 accounts=np.zeros((12,9)) if audit else None
 optional=(None,accounts,True) if audit else ()
 out=engine.sim(0,12,sv,sv,volume,fv,fv,volume,ev,a(-.002),a(0.),a(1e6),a(1e6),a(1e8),a(1e8),
  z,z,v*2,v*5,allowed,np.zeros_like(allowed),v*1000,z,fv,v*qty,a(10000),iv,a(.01),a(1),
  engine.mkp(**params),used,cum,ep,comp,diag,pos,sv,sv*1.02,sv*.98,fv,fv*1.02,fv*.98,z,z,v*(1e9 if notice_hour is None else notice_hour),v*1e9,
  xd,np.cumsum(ev,axis=0),a(1),np.ones((3,T,N)),v,*optional)
 return dict(out=out,pos=pos,used=used,cum=cum,diag=diag,ep=ep[:int(out[0][:,6].sum())],accounts=accounts)

class BoundaryTests(unittest.TestCase):
 def test_1000_contract_unit_quantum(self):self.assertEqual(str(paired_token_step('1','0.1','1000')),'100')
 def test_joint_step_is_not_simply_the_larger_step(self):self.assertEqual(str(paired_token_step('0.03','0.02','1')),'0.06')
 def test_lifecycle_notice_exits_and_prevents_reentry(self):
  r=replay(N=1,notice_hour=2)
  self.assertGreater(r['pos'][1,0],0.)
  self.assertEqual(r['pos'][2,0],0.);self.assertEqual(r['pos'][3,0],0.)
 def test_margin_moves_existing_A_cash_even_with_positive_B_wallet(self):
  aa,bb,ba,ab=E.rebalance_1x(500.,1000.,1200.)
  self.assertEqual((aa,bb,ba,ab),(300.,1200.,0.,200.))
  self.assertEqual(aa+bb,1500.)
 def test_cannot_withdraw_required_margin_or_unrealized_profit(self):
  aa,bb,ba,ab=E.rebalance_1x(-500.,2000.,1800.)
  self.assertEqual((aa,bb,ba,ab),(-300.,1800.,200.,0.))
 def test_joint_cash_shortfall_is_not_fabricated_away(self):
  aa,bb,ba,ab=E.rebalance_1x(100.,50.,200.)
  self.assertEqual((aa,bb,ba,ab),(0.,150.,0.,100.))
 def test_real_simulation_keeps_1x_collateral_after_funding_payment(self):
  r=replay(N=1,qty=10.,positive_payment=True,audit=True)
  ac=r['accounts'];self.assertGreater(ac[-1,8],0.)
  self.assertTrue((ac[:,1]+ac[:,2]+1e-8>=ac[:,3]).all())
  np.testing.assert_allclose(ac[:,0]-ac[:,4]+ac[:,1]+ac[:,2]-1000.,r['cum'],atol=1e-8)
 def test_strict_threshold(self):
  self.assertEqual(replay(entry_fav_n=.002)['pos'].max(),0.)
  self.assertGreater(replay(entry_fav_n=.001999)['pos'].max(),0.)
 def test_zero_inventory(self):self.assertEqual(replay(qty=0.)['pos'].max(),0.)
 def test_raw_dry_hour_has_no_fill(self):self.assertEqual(replay(dry=True)['pos'].max(),0.)
 def test_single_coin_cap_affects_negative_funding_leg(self):
  r=replay(per_coin_cap_fraction=.2)
  self.assertLessEqual(r['pos'].max(),200.+1e-8);self.assertAlmostEqual(r['pos'].max(),200.)
 def test_deployment_cap_is_shared(self):
  r=replay(deployment_fraction=.5)
  self.assertLessEqual(r['used'].max(),500.+1e-8);self.assertAlmostEqual(r['used'].max(),500.)
 def test_single_coin_mark_target_trims_after_a_visible_price_jump(self):
  r=replay(N=1,price_jump=True,per_coin_mtm_fraction=.2,derisk=1.,derisk_force=2.)
  self.assertAlmostEqual(r['pos'][1,0],200.)
  self.assertAlmostEqual(r['pos'][2,0],600.)
  self.assertAlmostEqual(r['pos'][3,0],200.)
 def test_mark_target_disabled_preserves_the_price_jump_exposure(self):
  r=replay(N=1,price_jump=True,per_coin_cap_fraction=.2,derisk=1.,derisk_force=2.)
  self.assertAlmostEqual(r['pos'][2,0],600.)
  self.assertAlmostEqual(r['pos'][3,0],600.)
 def test_native_zero_event_gap_is_reproduced_and_fixed(self):
  p=Path(__file__).resolve().parent.parent/'repro/lib/engine_v12s.py'
  spec=importlib.util.spec_from_file_location('native_comparison',p);native=importlib.util.module_from_spec(spec);spec.loader.exec_module(native)
  old=replay(engine=native,N=1,qty=10.,zero_event=True,k_imp=4.)
  new=replay(N=1,qty=10.,zero_event=True,k_imp=4.)
  self.assertEqual(old['out'][0][:,1].sum(),0.)
  self.assertAlmostEqual(new['out'][0][:,1].sum(),-3.9601,places=6)
 def test_exit_stays_locked_after_missing_quote(self):
  r=replay(N=1,missing_exit=True)
  self.assertGreater(r['pos'][1,0],0.)
  self.assertEqual(r['pos'][3,0],0.)
 def test_sigma_prefix_invariance(self):
  close=np.exp(np.arange(40)[:,None]*.01)*np.ones((1,2))
  close[:15,1]=np.nan
  first=causal_sigma(close)
  appended=np.vstack([close,np.full((12,2),1e12)])
  np.testing.assert_allclose(causal_sigma(appended)[:40],first,rtol=0,atol=0)

if __name__=='__main__':unittest.main(verbosity=2)
