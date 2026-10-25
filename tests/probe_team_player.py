"""Optional native B component probe. Real B code, fake model, no B source edits.

Flat deploy import is B's currently working entrypoint. This does NOT prove
its package import, PlayerAdapter/Coordinator integration, or a live LLM run.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine_client import reasoning_inputs
from integration import catalog


CHILD = r'''
import json, sys, tempfile
from pathlib import Path
from agent import LLMAgent
class ModelStub:
    def request(self, system, user, budget=700):
        data=json.loads(user)
        if "offer" in system:
            answer={"refuse":False,"offer":[x["id"] for x in data["my_information"]],"consideration":"model stub"}
        else:
            answer={"action":{"type":"stay","payload":{}},"evidence_used":[],"consideration":"model stub"}
        return json.dumps(answer), {"input":0,"output":0,"total":0}
data=json.load(sys.stdin)
with tempfile.TemporaryDirectory() as tmp:
    a=LLMAgent("native-b", ModelStub(),log_path=Path(tmp)/"probe.jsonl")
    a.sync(data["notice"], data["observation"])
    chosen=a.decide(data["notice"],data["observation"])
    offer, refused=a.respond_to_question("peer", "official record")
    assert chosen == ("stay",{})
    assert offer == ["clue:fixture"] and refused is False
    assert 1 not in a.belief.remaining("suspect")
    print(json.dumps({"status":"PASS","B_code":"native LLMAgent","model":"stub", "live_llm":False,
         "checks":["sync with canonical pools/dict profile", "two-value decide", "respond_to_question", "official evidence updates belief"]}))
'''


def main():
    b = ROOT/'teams/Agent_Reasoning'
    if not (b/'agent.py').exists(): b = b/'deploy'
    if not (b/'agent.py').exists():
        raise SystemExit('Clone B into ignored teams/Agent_Reasoning to run this optional probe')
    observation = {'game_id': 'probe', 'cycle_id': 1, 'phase': 'movement', 'phase_epoch': 1,
        'self': {'id': 'native-b', 'mode': 'active', 'job': {'id': 0, 'name': '탐정'},
                 'stat': 4, 'personality': 'cooperative', 'personal_goal': 'protect_secret', 'room': 0, 'prepared': False},
        'candidates': catalog()['candidates'], 'information': [
            {'id': 'clue:fixture', 'category': 'suspect', 'excluded_id': 1, 'text': '공식 배제 자료'}],
        'shared_information': [], 'positions': [], 'occupants': [], 'allowed_actions': ['move', 'stay'],
        'remaining_actions': 1, 'tasks': []}
    notice, obs = reasoning_inputs(observation)
    result = subprocess.run([sys.executable, '-B', '-c', CHILD], cwd=b,
        input=json.dumps({'notice': notice, 'observation': obs}, ensure_ascii=False),
        capture_output=True, text=True, encoding='utf-8', timeout=20,
        env=os.environ | {'PYTHONIOENCODING': 'utf-8'})
    if result.returncode:
        raise SystemExit(result.stderr)
    print(result.stdout.strip())


if __name__ == '__main__':
    main()
