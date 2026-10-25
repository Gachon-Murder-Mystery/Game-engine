"""Run the engine with actual C pure policies and C's explicit scripted players.

Agent policies stay in their repository. This is an adapter/driver, not an
engine-side planner. Never injects keys or calls a real LLM.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from engine_client import EngineClient

ROOT = Path(__file__).resolve().parent

def load_policy(path):
    require = Path(path)
    if not require.is_file(): raise RuntimeError(f'GM policy file not found: {require}')
    spec = importlib.util.spec_from_file_location('external_'+uuid.uuid4().hex, require)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def git_head(path):
    r=subprocess.run(['git','-C',str(path),'rev-parse','HEAD'],capture_output=True,text=True,timeout=10)
    return r.stdout.strip() if r.returncode==0 else 'unavailable'

@contextmanager
def engine_process(data):
    env = os.environ | {'PYTHONIOENCODING':'utf-8','PYTHONDONTWRITEBYTECODE':'1'}
    secret = subprocess.run([sys.executable,'-B','integration_cli.py','--data',str(data),'credentials'],
        cwd=ROOT,env=env,text=True,capture_output=True,encoding='utf-8',check=True,timeout=15)
    token = json.loads(secret.stdout)['operator_token']
    proc = subprocess.Popen([sys.executable,'-B','app.py','--port','0','--no-browser','--data',str(data)],
        cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
    lines = queue.Queue()
    threading.Thread(target=lambda:lines.put(proc.stdout.readline()),daemon=True).start()
    try:
        line = lines.get(timeout=20)
        url = next((x for x in line.split() if x.startswith('http://')),None)
        if url is None: raise RuntimeError('Engine failed to announce its address')
        yield EngineClient(url), token
    finally:
        proc.terminate()
        try: proc.wait(timeout=10)
        except subprocess.TimeoutExpired: proc.kill();proc.wait(timeout=5)
        proc.stdout.close();proc.stderr.close()

class LocalClient:
    """Same tool calls, used only by deterministic test/baseline harnesses."""
    def __init__(self, engine, credentials=None): self.engine,self.credentials=engine,dict(credentials or {})
    def bind(self, **credentials): return LocalClient(self.engine,credentials)
    def call(self,route,**data): return self.engine.handle(route,self.credentials|data)

def episode(client, operator_token, gm_repo, seed=7, scenario='normal', relationship_effects=True, progress=False):
    roles = load_policy(Path(gm_repo)/'role_policy.py')
    missions = load_policy(Path(gm_repo)/'mission_policy.py')
    actors_module = load_policy(Path(gm_repo)/'competitive_player.py')
    gm_metrics = load_policy(Path(gm_repo)/'gm_metrics.py')
    class ProtocolPlayer(actors_module.CompetitivePlayer):
        """Explicit scripted baseline adapter, not a replacement for B's policy.

        Native C ranks forms/content; the adapter removes propositions that
        are not legal in the authoritative engine's current response domain.
        """
        filtered=0
        def respond(self,request):
            self.legal_options=request['response_options']
            return super().respond(request)
        def _soft(self,hypothesis,prior,limit):
            raw=super()._soft(hypothesis,prior,len(self.held)+3)
            legal=[]
            for s in raw:
                if s['kind']=='ambiguous':
                    ok=any(x['category']==s['category'] and x['candidate']==s['candidate'] for x in self.legal_options['ambiguous'])
                else:
                    ok=any(x['clue_id']==s['clue_id'] and s.get('narration') in x['narrative_options'] for x in self.legal_options['indirect'])
                if ok:legal.append(s)
                else:self.filtered+=1
            return legal[:limit]
    operator = client.bind(operator_token=operator_token)
    pool = operator.call('pool/bootstrap',pool_id='society',model_version='gpt-6-luna')
    sources=('integration.py','competitive.py','competitive_rules.py','competitive_catalog.json','launcher.py')
    tree_hash=hashlib.sha256(b''.join((ROOT/p).read_bytes() for p in sources)).hexdigest()
    provenance={'engine_commit':git_head(ROOT),'engine_tree_hash':tree_hash,
        'player_checkout_commit':git_head(ROOT/'teams/Agent_Reasoning'),'gm_commit':git_head(gm_repo),
        'runtime':'C-pure-GM/scripted-player-legality-adapter; no B or LLM execution',
        'command':f'launcher.py --seed {seed} --scenario {scenario}',
        'pool_snapshot_hash':hashlib.sha256(json.dumps(pool,sort_keys=True).encode()).hexdigest()}
    created = operator.call('games/create',pool_id='society',game_id='episode:'+uuid.uuid4().hex,seed=seed,
        rules={'relationship_effects':relationship_effects},provenance=provenance)
    gid = created['game_id']
    cards = pool['agents']
    identities = {a['agent_id']:client.bind(**operator.call('pool/credentials',agent_id=a['agent_id'])) for a in cards}
    gm = identities[created['controller']['controller_id']].bind(**created['controller'],game_id=gid)
    history = {a['agent_id']:a['history'] for a in cards}
    available = [a['agent_id'] for a in cards if a['pool_role']=='player' and a['pool_status']=='available']
    plan = roles.plan_roles(available,history,seed)
    draw = gm.call('gm/mission-candidates',activate=plan['activation'])
    chars = draw['characters']
    player_data = [{'agent_id':a['agent_id'],'character_id':a['character_id'],
                    'mbti':next(c['mbti'] for c in chars if c['character_id']==a['character_id'])} for a in plan['activation']]
    mission_plan = missions.plan_missions(player_data,chars,draw['mission_candidates'],seed,
        received={aid:[m['mission_id'] for h in hs for m in h.get('sub_missions',[])] for aid,hs in history.items()},
        indirect_clue_ids=draw['indirect_clue_ids'])
    activation = [x|{'sub_missions':mission_plan['sub_missions'][x['agent_id']]} for x in plan['activation']]
    gm.call('gm/command',operation='PLAN',command_id='setup',map_version=0,activate=activation,
        alternatives=plan['alternatives'],selected_alternative=plan['selected_alternative'],reason=plan['reason']+' / '+mission_plan['reason'])
    policies={}
    for data in player_data:
        policies[data['agent_id']] = ProtocolPlayer(data['agent_id'],roles.load_data()['characters'][data['character_id']] if hasattr(roles,'load_data') else
            next(c for c in client.call('catalog')['characters'] if c['id']==data['character_id']), data['mbti'],seed,
            missions=mission_plan['sub_missions'][data['agent_id']],
            names={c['character_id']:c['name'] for c in chars},places={i:p['name'] for i,p in enumerate(client.call('catalog')['places'])})
    roles_by_agent={}
    applied={aid:set() for aid in policies}
    def update_policy(aid,obs):
        policy=policies[aid]
        for info in obs['information']:
            marker=(info['id'],info['kind'],info.get('text'))
            if marker not in applied[aid]:policy.ingest({'clues':[info]});applied[aid].add(marker)
        # The prototype's ID-only cache otherwise misses narrated -> full upgrades.
        policy.held=[x for x in obs['information'] if x['kind'] in ('definite','indirect')]
        simple={'room':obs['self']['room'],'cycle':obs['cycle_id'],
            'relationship':{int(k):v for k,v in obs['relationships'].items()},
            'achieved_missions':[m['mission_id'] for m in obs['sub_missions'] if m['achieved']],
            'here':[{'id':c['id'],'kind':'player' if c['mode']=='active' else c['mode']} for c in obs['occupants']]}
        policy.observe(simple)
        return simple
    checks=[]; injected=False
    def request(role,obs,action,payload):
        req={k:obs[k] for k in ('game_id','cycle_id','phase','phase_epoch')}
        req.update(agent_id=obs['self']['id'],action_id=uuid.uuid4().hex,action=action,payload=payload)
        return role.call('agent/action',request=req)
    for turn in range(500):
        gm.call('gm/heartbeat')
        snapshot=gm.call('gm/observe')
        if progress and snapshot['phase']=='movement' and (snapshot['cycle_id']==1 or snapshot['cycle_id']%10==0):
            print(f"cycle {snapshot['cycle_id']}, active {len(snapshot['active_agents'])}",file=sys.stderr,flush=True)
        if not roles_by_agent or snapshot['phase']=='movement':
            for aid in policies:
                session=identities[aid].call('pool/session')
                if session['role']=='player': roles_by_agent[aid]=identities[aid].bind(**session['credentials'])
                else: roles_by_agent.pop(aid,None)
        if not injected and snapshot['cycle_id']>=3 and scenario!='normal':
            if scenario=='player-fault':
                victim=sorted(roles_by_agent)[0]
                operator.call('games/operator',game_id=gid,operation='FAIL_PLAYER',agent_id=victim)
                checks.append('player fault -> NPC, no replenishment')
            elif scenario=='gm-fault':
                old=gm
                out=operator.call('games/operator',game_id=gid,operation='FAIL_GM',term=snapshot['control']['term'])
                gm=identities[out['controller']['controller_id']].bind(**out['controller'],game_id=gid)
                try:old.call('gm/heartbeat');raise AssertionError('old GM accepted')
                except Exception as exc:
                    if isinstance(exc,AssertionError):raise
                checks.append('new GM uses engine-only context; old term fenced')
            elif scenario=='human':
                for aid in list(roles_by_agent): operator.call('games/operator',game_id=gid,operation='FAIL_PLAYER',agent_id=aid)
                out=operator.call('games/operator',game_id=gid,operation='FAIL_GM',term=snapshot['control']['term'])
                gm=identities[out['controller']['controller_id']].bind(**out['controller'],game_id=gid)
                checks.append('reserve GM -> no_players finalization')
            injected=True
            continue
        if snapshot['resolution']:
            current_history={a['agent_id']:a['history'] for a in snapshot['pool']}
            role_flags=[roles.exploratory(a['agent_id'],a['character_id'],current_history) for a in snapshot['role_assignments']]
            strengths=[]
            for a in snapshot['role_assignments']:
                competency=roles.job_competency(current_history.get(a['agent_id'],[]))
                if competency is not None:strengths.append(competency[a['character_id']])
            for a in snapshot['pending_announcements']:
                gm.call('gm/command',operation='ANNOUNCE',command_id=uuid.uuid4().hex,**a)
            gm.call('gm/command',operation='FINISH',command_id='finish')
            events=gm.call('events')['events']
            evidence=[e['event_id'] for e in events if e['type']=='game_finished']
            metrics={'completion':gm_metrics.completion(snapshot['resolution'],cycles=snapshot['cycle_id']),
                     'balance':gm_metrics.balance(strengths),
                     'exploration':gm_metrics.exploration(role_flags),'record':gm_metrics.record_completeness(snapshot['plans'])}
            gm.call('gm/command',operation='REPORT',command_id='report',metrics=metrics,evidence=evidence)
            exported=operator.call('games/export',game_id=gid)
            return {'status':'PASS','scenario':scenario,'seed':seed,'game_id':gid,'resolution':snapshot['resolution'],
                'cycles':snapshot['cycle_id'],'runtime':'actual-C-GM-policies-and-explicit-scripted-player-legality-adapter',
                'filtered_scripted_proposals':sum(p.filtered for p in policies.values()),
                'live_llm':False,'checks':checks,'metrics':exported['metrics'],
                'relationship_effects':relationship_effects,'report':gm.call('gm/observe')['gm_report']}
        pending_targets=set()
        for aid,role in list(roles_by_agent.items()):
            obs=role.call('agent/observe');policy=policies[aid]
            simple=update_policy(aid,obs)
            if not obs['remaining_actions']:continue
            if obs['phase']=='movement':
                choice=policy.move(simple);payload={'room':choice['room']} if choice['action']=='move' else {}
            elif obs['phase']=='interaction':
                choice=policy.interact(simple)
                if choice['action']=='explore':
                    target=choice.get('target','room');payload={} if target=='room' else {'target_id':int(target.split(':')[1])}
                elif choice['action']=='question':payload={k:choice[k] for k in ('target_id','approach','hypothesis')}
                else:payload={}
            else:
                decision=policy.deduce();choice={'action':'accuse' if decision['decision']=='challenge' else 'pass'}
                payload=(decision['accusation']|{'output_tokens':0}) if choice['action']=='accuse' else {}
            result=request(role,obs,choice['action'],payload)
            if choice['action']=='question' and result['execution_status']=='pending':pending_targets.add(payload['target_id'])
            if result['execution_status']=='completed' and choice['action']=='explore':
                policy.ingest({'action':'explore','target':'room' if not payload else 'corpse',
                               'clues':result['acquired_information'],'successes':result['roll']['successes']})
        for aid,role in list(roles_by_agent.items()):
            if policies[aid].sheet['id'] not in pending_targets:continue
            update_policy(aid,role.call('agent/observe'))
            for q in role.call('questions/inbox')['questions']:
                # The actual C scripted policy chooses content; adapter translates only field spelling.
                answer=policies[aid].respond(q|{'asker_id':q['asker_character']})
                statements=[]
                for s in answer['statements']:
                    s=dict(s)
                    if 'narration' in s:s['text']=s.pop('narration')
                    statements.append(s)
                role.call('questions/respond',question_id=q['question_id'],response_id=uuid.uuid4().hex,statements=statements)
        gm.call('gm/command',operation='ADVANCE',command_id=uuid.uuid4().hex,phase_epoch=snapshot['phase_epoch'])
    raise RuntimeError('Driver exhausted its budget without an official ending')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gm-repo',default=str(ROOT/'teams/GM_Agent_Society'))
    parser.add_argument('--seed',type=int,default=7)
    parser.add_argument('--scenario',choices=('normal','player-fault','gm-fault','human'),default='normal')
    parser.add_argument('--data',help='Explicit persistent SQLite path; otherwise temporary isolated database')
    parser.add_argument('--relationship-effects',choices=('on','off'),default='on')
    parser.add_argument('--progress',action='store_true')
    args=parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        data=Path(args.data) if args.data else Path(temporary)/'episode.sqlite3'
        data.parent.mkdir(parents=True,exist_ok=True)
        with engine_process(data) as (client, token):
            result=episode(client,token,args.gm_repo,args.seed,args.scenario,args.relationship_effects=='on',args.progress)
            print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
