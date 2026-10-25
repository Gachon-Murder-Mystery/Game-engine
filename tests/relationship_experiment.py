"""Paired engine-rule experiment, NOT evidence of LLM social learning."""
import copy
import json
import sys
import tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from integration import ExternalEngine
from competitive import Rules

def experiment(trials=1024):
    with tempfile.TemporaryDirectory() as directory:
        e=ExternalEngine(str(Path(directory)/'rules.db'))
        op={'operator_token':e.operator_token}
        e.handle('pool/bootstrap',op)
        created=e.handle('games/create',op|{'game_id':'experiment','seed':17})
        with e.transaction() as db:g=e.load(db,'games','experiment','id')
        g['roll_secret']='paired-engine-experiment-fixed-randomness'
        rules=Rules(e);rates={};npc={}
        for effects in (True,False):
            for value in (20,50,80):
                state=copy.deepcopy(g)
                state['rules']['relationship_effects']=effects
                state['relationships']['0']['3']=value
                successes=0
                for i in range(trials):
                    r=rules.roll(state,state['characters']['0'],'question',state['characters']['3'],str(i))
                    successes+=r['successes']>0
                rates[('on' if effects else 'off')+':'+str(value)]=successes/trials
        for value in (20,50,80):
            state=copy.deepcopy(g);state['characters']['3']['mbti']='ESFJ';state['relationships']['3']['0']=value
            full=0
            for i in range(trials):
                q={'question_id':'trial:'+str(i),'asker_character':0,'target_id':3,'approach':'question',
                   'successes':2,'hypothesis':{'suspect':0,'weapon':0,'place':0}}
                full+=any(x['kind']=='definite' for x in rules.default_response(state,q))
            npc[str(value)]=full/trials
        assert rates['on:20']<rates['on:50']<rates['on:80']
        assert rates['off:20']==rates['off:50']==rates['off:80']
        assert npc['20']<npc['50']<npc['80']
        return {'status':'PASS','trials_per_condition':trials,'live_llm':False,
            'design':'same sheet, same random draws, only relationship value/effect switch differs',
            'question_success_rates':rates,'F_NPC_definite_disclosure_rates':npc,
            'interpretation':'causal effect of engine rules only; no claim about LLM communication or learning'}

if __name__=='__main__':print(json.dumps(experiment(),ensure_ascii=False,indent=2))
