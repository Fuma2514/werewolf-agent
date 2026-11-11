import importlib.util, random, collections
spec=importlib.util.spec_from_file_location('agent','main.py'); a=importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
src=open('sim.py').read().split('rng = random.Random')[0]
exec(src)
rng=random.Random(11)
vill=[0,0]; acc=[0,0]
for i in range(200):
    role=(['seer']*10+['doctor']*10+['villager']*10)[i%30]
    g=Game(random.Random(rng.randrange(1<<30)),role)
    def god_decide(p,seed='s',g=g):
        d=p['decision']
        wolves=[s for s in range(1,9) if g.role_of[s]=='werewolf' and g.alive[s] and s!=g.me]
        if d=='speak':
            t=wolves[0] if wolves else None
            return {'text':'Seat %d is a wolf. Vote seat %d.'%(t,t)} if t else {'pass':True}
        if d=='vote':
            t=wolves[0] if wolves else None
            if t: acc[0]+=1; acc[1]+=1
            return {'target': t}
        return {'target': None}
    a.decide=god_decide
    out=g.run(random.Random(i))
    a.decide=a.decide.__wrapped__ if hasattr(a.decide,'__wrapped__') else a.decide
    vill[0]+= out['winner']=='village'; vill[1]+=1
    import importlib as _i; _i.reload(a); a.decide=god_decide
print('god-mode village WR: %d/%d = %.2f'%(vill[0],vill[1],vill[0]/max(1,vill[1])))
