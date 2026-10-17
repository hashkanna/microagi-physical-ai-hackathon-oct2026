"""Robustness sweep: random reachable target-bin placements (random yaw) on the long-bench sample; prints success by reach margin."""
import copy
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cellcheck.schema import Scene
from cellcheck.sim import run_task
from cellcheck.reach import reach_grid
from cellcheck.compiler import precheck, SceneError
random.seed(1)
base = Scene.model_validate(json.load(open('scenes/unreachable.json')))
g = reach_grid(base)
cells = [(g['x0']+i*g['step'], g['y0']+j*g['step'], j, i) for j,row in enumerate(g['reachable']) for i,v in enumerate(row) if v]
def margin(j,i):
    R=g['reachable']; m=0
    for k in range(1,6):
        if all(0<=j+a<len(R) and 0<=i+b<len(R[0]) and R[j+a][i+b] for a in (-k,0,k) for b in (-k,0,k)): m=k
        else: break
    return m
stats={}
t=time.time(); n=0
random.shuffle(cells)
for x,y,j,i in cells:
    s=copy.deepcopy(base); b=s.obj('bin_b'); b.x, b.y = round(x,3), round(y,3); b.yaw_deg=random.choice([0,20,-30,45])
    try: precheck(s)
    except SceneError: continue
    r=run_task(s); n+=1
    m=margin(j,i)
    st=stats.setdefault(min(m,2),[0,0,[]]); st[0]+=r.success; st[1]+=1
    if not r.success: st[2].append(((round(x,2),round(y,2)), (r.failure_reason or '')[:70]))
    if n>=36: break
print('time', round(time.time()-t,1))
for m,(ok,tot,f) in sorted(stats.items()):
    print(f'reach margin {m} cells: {ok}/{tot} success'); [print('   ',x) for x in f[:4]]
