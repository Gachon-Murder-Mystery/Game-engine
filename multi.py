"""Authoritative multi-player contracts and an asynchronous, resumable supervisor.

Credentials live outside observation payloads. Agent policies never receive truth.
Only engine actions commit facts; the supervisor allocates work and consumes events.
"""
import copy
import hashlib
import json
import math
import random
import secrets
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import threading

from engine import new_game, JOBS, CAT, PLACES

TRAITS = ['협력적', '신중함', '독립적', '경쟁적']
GOALS = ['protect_secret', 'prove_innocence', 'steal_vault']
CHOICES = {'INVESTIGATE', 'SHARE_FULL', 'SHARE_PARTIAL', 'WITHHOLD',
           'REQUEST', 'VERIFY', 'CHALLENGE', 'STEAL', 'ACCUSE'}


def create(count=3, seed=0):
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 12:
        raise ValueError('에이전트 수는 1~12명입니다.')
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32:
        raise ValueError('seed는 0~4294967295 정수입니다.')
    rng = random.Random(seed)
    base = new_game()
    base['truth'] = {k: rng.randrange(len(v)) for k, v in CAT.items()}
    # Rebuild physical evidence deterministically, without assigning agents as suspects.
    clues = []
    for category, values in CAT.items():
        for index, name in enumerate(values):
            if index != base['truth'][category]:
                clues.append({'id': f'{category}:{index}', 'category': category,
                              'index': index, 'source_room': (len(clues)*3+2) % 8,
                              'text': f'자료 대조로 {name}을(를) {category} 후보에서 제외합니다.'})
    agents = {}
    for i in range(count):
        aid = f'agent-{i+1}'
        agents[aid] = {'id': aid, 'job': rng.choice(JOBS), 'trait': rng.choice(TRAITS),
                       'goal': rng.choice(GOALS), 'status': 'active', 'heartbeat': time.time(),
                       'private': [], 'belief': {}, 'decisions': [], 'withheld': [],
                       'goal_achieved': False, 'token': secrets.token_urlsafe(32)}
    g = {'kind': 'multi', 'version': 1, 'id': base['id'], 'seed': seed,
            'gm_token': secrets.token_urlsafe(32), 'truth': base['truth'], 'all_clues': clues,
            'agents': agents, 'shared': [], 'tasks': [
                {'id': f'room-{r}', 'room': r, 'status': 'pending', 'owner': None,
                 'generation': 0, 'dependencies': [], 'evidence_ids': []} for r in range(8)],
            'events': [], 'actions': {}, 'messages': [], 'ownership': [],
            'ended': False, 'won': False, 'wrong': 0, 'cycle': 0,
            'started_at': time.time(), 'finished_at': None, 'status': 'ready',
            'metrics': {'reassignments': 0, 'actions': 0, 'latency_total': 0,
                        'sharing': 0, 'partial_sharing': 0, 'withholding': 0,
                        'requests': 0, 'verification': 0, 'challenges': 0}}
    allocate(g)
    return g


def authorize(g, token, agent_id=None):
    if agent_id is not None and not isinstance(agent_id,str):
        raise ValueError('agent_id 형식을 확인하세요.')
    expected = g['agents'].get(agent_id, {}).get('token') if agent_id else g['gm_token']
    if not isinstance(token, str) or not expected or not secrets.compare_digest(token, expected):
        raise ValueError('접근 권한이 없습니다.')


def board(evidence):
    excluded = {c['id'] for c in evidence}
    return {k: [{'id': i, 'name': name, 'excluded': f'{k}:{i}' in excluded}
                for i, name in enumerate(values)] for k, values in CAT.items()}


def view(g, agent_id=None):
    """GM gets only shared evidence; each player additionally gets their own state."""
    result = {k: copy.deepcopy(g[k]) for k in
              ('id', 'seed', 'tasks', 'shared', 'events', 'messages', 'ownership',
               'ended', 'won', 'cycle', 'status')}
    result['agents'] = [{'id': a['id'], 'job': a['job'], 'trait': a['trait'],
                         'status': a['status']} for a in g['agents'].values()]
    result['board'] = board(g['shared'])
    result['places'] = PLACES
    result['metrics'] = copy.deepcopy(g['metrics'])
    result['metrics']['active_agents'] = sum(a['status'] == 'active' for a in g['agents'].values())
    result['metrics']['completed_tasks'] = sum(t['status'] == 'completed' for t in g['tasks'])
    result['metrics']['task_loss_count'] = 0  # Tasks are never deleted.
    if g['finished_at']:
        result['metrics']['time_to_solution'] = g['finished_at']-g['started_at']
    if agent_id:
        a = g['agents'][agent_id]
        result['self'] = {k: copy.deepcopy(a[k]) for k in
                          ('id', 'job', 'trait', 'goal', 'private', 'belief',
                           'decisions', 'goal_achieved')}
        result['board'] = board(g['shared'] + a['private'])
        result['messages'] = [m for m in result['messages']
                              if m['scope'] == 'shared' or agent_id in (m['sender'], m['recipient'])]
    else:
        result['messages'] = [m for m in result['messages'] if m['scope'] == 'shared']
    return result


def refresh(g):
    shared = {c['id'] for c in g['shared']}
    for task in g['tasks']:
        ids = {c['id'] for c in g['all_clues'] if c['source_room'] == task['room']}
        task['evidence_ids'] = sorted(ids & shared)
        if ids <= shared:
            task['status'] = 'completed'
            task['owner'] = None


def allocate(g):
    refresh(g)
    active = [a['id'] for a in g['agents'].values() if a['status'] == 'active']
    if not active:
        g['status'] = 'waiting_for_human'
        return
    for aid in active:
        if any(t['owner'] == aid and t['status'] == 'assigned' for t in g['tasks']):
            continue
        task = next((t for t in g['tasks'] if t['status'] == 'pending'), None)
        if task:
            task.update(status='assigned', owner=aid, generation=task['generation']+1)
            g['ownership'].append({'task_id': task['id'], 'owner': aid,
                                   'generation': task['generation'], 'reason': 'assignment'})


def fail(g, aid, reason='injected_failure'):
    if not isinstance(aid,str) or aid not in g['agents']:
        raise ValueError('알 수 없는 에이전트입니다.')
    a = g['agents'][aid]
    if a['status'] == 'failed':
        return
    a['status'] = 'failed'
    g['events'].append({'seq': len(g['events'])+1, 'game_id': g['id'],
                         'action_id': None, 'agent_id': aid, 'task_id': None,
                         'status': 'failed', 'reason': reason, 'shared_evidence_ids': [],
                         'timestamp': time.time()})
    for task in g['tasks']:
        if task['owner'] == aid and task['status'] != 'completed':
            task.update(owner=None, status='pending', generation=task['generation']+1)
            g['metrics']['reassignments'] += 1
            g['ownership'].append({'task_id': task['id'], 'owner': None,
                                   'generation': task['generation'], 'reason': reason})
    allocate(g)


def execute(g, request):
    """Direct engine entry point, called under a short state commit lock.

    action_id is idempotent. A reassigned task's stale generation cannot commit.
    Private outcomes are retained only in the player's cached result, not GM events.
    """
    aid = request.get('agent_id')
    if not isinstance(aid,str) or aid not in g['agents']:
        raise ValueError('알 수 없는 에이전트입니다.')
    action_id = request.get('action_id')
    if not isinstance(action_id, str) or not 1 <= len(action_id) <= 100:
        raise ValueError('action_id가 필요합니다.')
    fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    if action_id in g['actions']:
        old = g['actions'][action_id]
        if old['fingerprint'] != fingerprint:
            raise ValueError('같은 action_id에 다른 요청을 사용할 수 없습니다.')
        return copy.deepcopy(old['result'])
    a = g['agents'][aid]
    if a['status'] != 'active' or g['ended']:
        raise ValueError('현재 행동할 수 없는 상태입니다.')
    action = request.get('action')
    if not isinstance(action,str) or action not in CHOICES:
        raise ValueError('지원하지 않는 행동입니다.')
    utility = request.get('utility', {})
    if not isinstance(utility,dict) or any(k not in ('team','private','information','cost','risk')
            or isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v)
            for k,v in utility.items()):
        raise ValueError('utility는 유한한 수치 요소만 허용합니다.')
    task = next((t for t in g['tasks'] if t['id'] == request.get('task_id')), None)
    if request.get('task_id') is not None and not task:
        raise ValueError('알 수 없는 작업입니다.')
    generation=request.get('generation')
    if action in ('INVESTIGATE','STEAL') and (isinstance(generation,bool) or not isinstance(generation,int)):
        raise ValueError('작업 세대는 정수여야 합니다.')
    if action == 'INVESTIGATE' and (not task or task['owner'] != aid or
            task['status'] != 'assigned' or request.get('generation') != task['generation']):
        raise ValueError('작업 소유권 또는 세대가 변경되었습니다.')
    refs = request.get('evidence_ids', [])
    if not isinstance(refs, list) or any(not isinstance(x, str) for x in refs):
        raise ValueError('근거 ID 형식을 확인하세요.')
    known = {c['id']: c for c in g['shared'] + a['private']}
    if any(ref not in known for ref in refs):
        raise ValueError('접근할 수 없는 근거입니다.')
    result = {'action_id': action_id, 'agent_id': aid, 'status': 'completed', 'observations': []}
    shared_refs = []
    if action == 'INVESTIGATE':
        # All agents have identical dice/stat ability. Draws depend on per-agent
        # investigation count, so concurrent ordering does not change their rolls.
        attempt = sum(d['action'] == 'INVESTIGATE' for d in a['decisions'])
        rng = random.Random(f"{g['seed']}:{aid}:{attempt}")
        dice = [rng.randint(1, 6) for _ in range(2)]
        result['dice'] = dice
        successes = sum(v > 3 for v in dice)
        pool = [c for c in g['all_clues'] if c['source_room'] == task['room'] and c['id'] not in known]
        got = copy.deepcopy(pool[:int(successes > 0)])
        a['private'].extend(got)
        result['observations'] = got
        result['successes'] = successes
    elif action in ('SHARE_FULL', 'SHARE_PARTIAL'):
        if any(ref not in {c['id'] for c in a['private']} for ref in refs):
            raise ValueError('자신이 보유한 증거만 공유할 수 있습니다.')
        selected = refs[:1] if action == 'SHARE_PARTIAL' else refs
        for ref in selected:
            if ref not in {c['id'] for c in g['shared']}:
                g['shared'].append(copy.deepcopy(known[ref]))
            shared_refs.append(ref)
        g['metrics']['sharing' if action == 'SHARE_FULL' else 'partial_sharing'] += 1
        if a['goal'] == 'prove_innocence' and any(known[r]['category'] == 'suspect' for r in selected):
            a['goal_achieved'] = True
    elif action == 'WITHHOLD':
        a['withheld'] = sorted(set(a['withheld']) | set(refs))
        a['goal_achieved'] = a['goal_achieved'] or (a['goal'] == 'protect_secret' and bool(refs))
        g['metrics']['withholding'] += 1
    elif action in ('REQUEST', 'VERIFY', 'CHALLENGE'):
        recipient = request.get('recipient')
        scope = request.get('scope', 'shared')
        if recipient is not None and (not isinstance(recipient,str) or recipient not in g['agents']):
            raise ValueError('수신자를 확인하세요.')
        if scope not in ('shared', 'private') or (scope == 'private' and not recipient):
            raise ValueError('메시지 공개 범위를 확인하세요.')
        if scope == 'shared' and any(r not in {c['id'] for c in g['shared']} for r in refs):
            raise ValueError('공유되지 않은 증거를 공개 메시지에서 참조할 수 없습니다.')
        g['messages'].append({'sender': aid, 'recipient': recipient, 'scope': scope,
                              'action': action, 'evidence_ids': refs})
        result['observations'] = [copy.deepcopy(known[r]) for r in refs] if action != 'REQUEST' else []
        g['metrics'][{'REQUEST': 'requests', 'VERIFY': 'verification', 'CHALLENGE': 'challenges'}[action]] += 1
    elif action == 'STEAL':
        if not task or task['owner'] != aid or task['room'] != 5 or request.get('generation') != task['generation']:
            raise ValueError('금고 칸의 작업을 맡은 에이전트만 시도할 수 있습니다.')
        a['goal_achieved'] = a['goal'] == 'steal_vault'
        result['observations'] = [{'text': '금고의 개인 소지품을 확보했습니다.'}]
    elif action == 'ACCUSE':
        guess = request.get('guess')
        if not isinstance(guess, dict) or set(guess) != set(CAT) or any(
                isinstance(guess[k], bool) or not isinstance(guess[k], int) or
                not 0 <= guess[k] < len(CAT[k]) for k in CAT):
            raise ValueError('범인·흉기·장소를 모두 지정하세요.')
        g['won'] = guess == g['truth']
        g['wrong'] += int(not g['won'])
        g['ended'] = g['won'] or g['wrong'] >= 2
        result['won'] = g['won']
        if g['ended']:
            g['status'] = 'solved' if g['won'] else 'unsolved'
            g['finished_at'] = time.time()
    # Private decision rationales are never copied into public completion events.
    a['decisions'].append({'action_id': action_id, 'action': action,
                           'evidence_ids': refs, 'reason': str(request.get('reason', ''))[:1000],
                           'utility': request.get('utility', {}), 'cycle': g['cycle']})
    a['belief'] = {k: [x['id'] for x in v if not x['excluded']]
                   for k, v in board(g['shared'] + a['private']).items()}
    a['heartbeat'] = time.time()
    refresh(g)
    g['metrics']['actions'] += 1
    g['events'].append({'seq': len(g['events'])+1, 'game_id': g['id'],
                         'action_id': action_id, 'agent_id': aid,
                         'task_id': request.get('task_id'), 'status': 'completed',
                         'shared_evidence_ids': shared_refs, 'timestamp': time.time()})
    g['actions'][action_id] = {'fingerprint': fingerprint, 'result': copy.deepcopy(result)}
    return result


def decide(observation, gateway=None):
    """One reusable policy for all n agents; LLM sees only its authorized view."""
    a = observation['self']
    task = next((t for t in observation['tasks'] if t['owner'] == a['id']), None)
    shared = {c['id'] for c in observation['shared']}
    fresh = [c['id'] for c in a['private'] if c['id'] not in shared]
    remaining = {k: [x['id'] for x in v if not x['excluded']]
                 for k, v in board(observation['shared']).items()}
    action = 'REQUEST'
    reason = '미완료 작업 또는 공유 증거를 요청한다.'
    refs = []
    if all(len(v) == 1 for v in remaining.values()):
        action = 'ACCUSE'; reason = '공유된 제외 증거가 세 후보를 하나씩 남겼다.'
    elif fresh:
        refs = fresh
        if a['goal'] == 'protect_secret' and not any(d['action'] == 'WITHHOLD' for d in a['decisions']):
            action = 'WITHHOLD'; reason = '개인 비밀 보호를 위해 이번 사이클의 공개를 보류한다.'
        else:
            action = 'SHARE_PARTIAL' if a['trait'] == '신중함' and len(fresh) > 1 else 'SHARE_FULL'
            reason = '수사 정체와 증거 손실 위험을 줄이기 위해 보유 증거를 공유한다.'
    elif task and task['room'] == 5 and a['goal'] == 'steal_vault' and not a['goal_achieved']:
        action = 'STEAL'; reason = '맡은 금고 조사 중 개인 목표를 수행한다.'
    elif task:
        action = 'INVESTIGATE'; reason = '할당된 장소에서 아직 알려지지 않은 물증을 확인한다.'
    proposal = {'action': action, 'evidence_ids': refs, 'reason': reason,
                'utility': {'team': 1 if action != 'WITHHOLD' else 0,
                            'private': int(action in ('WITHHOLD', 'STEAL')),
                            'information': int(action == 'INVESTIGATE'), 'cost': 1, 'risk': 0}}
    if gateway and gateway.key:
        try:
            raw = gateway.request(
                '당신은 수사 플레이어다. 허용된 관찰만 사용한다. JSON 객체로 action, evidence_ids, reason, '
                'utility를 반환한다. 행동은 INVESTIGATE/SHARE_FULL/SHARE_PARTIAL/WITHHOLD/REQUEST/VERIFY/CHALLENGE/STEAL/ACCUSE. '
                '공동 수사와 개인 목표를 비교하되 이미 수집한 증거를 검토하고 진행 정체를 피하라.',
                json.dumps({'observation': observation, 'baseline': proposal}, ensure_ascii=False), 700)
            parsed = json.loads(raw)
            if not isinstance(parsed,dict) or parsed.get('action') not in CHOICES:
                raise ValueError('잘못된 모델 행동')
            proposal = {k: parsed[k] for k in ('action', 'evidence_ids', 'reason', 'utility') if k in parsed}
        except Exception:
            # Gateway failures are recorded privately; the deterministic policy keeps running.
            proposal['reason'] += ' (모델 응답 오류로 기본 정책 사용)'
    proposal.update(agent_id=a['id'], action_id=secrets.token_urlsafe(18))
    if task:
        proposal.update(task_id=task['id'], generation=task['generation'])
    if proposal['action'] == 'ACCUSE':
        proposal['guess'] = {k: v[0] for k, v in remaining.items() if len(v) == 1}
    return proposal


class Runtime:
    """Workers perform reasoning outside the DB/game lock; only commits serialize."""
    def __init__(self, store):
        self.store = store
        self.pool = ThreadPoolExecutor(max_workers=12, thread_name_prefix='player')
        self.controllers = ThreadPoolExecutor(max_workers=4, thread_name_prefix='supervisor')
        self.running = {}
        self.jobs_lock = threading.Lock()

    def start(self, game_id, gateway=None, cycles=200):
        with self.jobs_lock:
            if game_id in self.running and not self.running[game_id].done():
                return
            self.running[game_id] = self.controllers.submit(self.run, game_id, gateway, cycles)

    def worker(self, game_id, aid, gateway):
        start = time.monotonic()
        with self.store.for_game(game_id):
            g = self.store.get(game_id)
            if g['agents'][aid]['status'] != 'active' or g['ended']:
                return
            observation = view(g, aid)
        proposal = decide(observation, gateway)
        with self.store.for_game(game_id):
            g = self.store.get(game_id)
            try:
                execute(g, proposal)
            except ValueError:
                # Invalid model proposals use a fresh authorized observation and
                # the baseline. Delayed failed workers cannot commit anything.
                if g['agents'][aid]['status']=='active' and not g['ended']:
                    fallback=decide(view(g,aid))
                    fallback['reason'] += ' (엔진 검증 후 기본 정책 사용)'
                    execute(g,fallback)
            g['metrics']['latency_total'] += time.monotonic()-start
            self.store.save(g)

    def run(self, game_id, gateway=None, cycles=200):
        jobs = {}
        submitted = 0
        budget = cycles * len(self.store.get(game_id)['agents'])
        while submitted < budget or jobs:
            with self.store.for_game(game_id):
                g = self.store.get(game_id)
                if g['ended'] or g['status'] == 'paused':
                    return
                allocate(g)
                active = [a['id'] for a in g['agents'].values() if a['status'] == 'active']
                if not active:
                    self.store.save(g)
                    return
                g['status'] = 'running'
                self.store.save(g)
            occupied = {aid for aid, _ in jobs.values()}
            for aid in active:
                if aid not in occupied and submitted < budget:
                    jobs[self.pool.submit(self.worker, game_id, aid, gateway)] = (aid, time.monotonic())
                    submitted += 1
            if not jobs:
                break
            # A fast player can take its next action while another player reasons.
            done, _ = wait(jobs, timeout=.1, return_when=FIRST_COMPLETED)
            with self.store.for_game(game_id):
                g = self.store.get(game_id)
                if done:
                    g['cycle'] += 1
                for job in done:
                    aid, _ = jobs.pop(job)
                    if job.exception():
                        fail(g, aid, 'worker_error')
                for job, (aid, started) in list(jobs.items()):
                    if time.monotonic()-started > 40:
                        fail(g, aid, 'timeout')
                        job.cancel()
                        del jobs[job]
                self.store.save(g)
        with self.store.for_game(game_id):
            g = self.store.get(game_id)
            if not g['ended'] and g['status'] == 'running':
                g['status'] = 'paused'
            self.store.save(g)

    def close(self):
        self.controllers.shutdown(wait=True)
        self.pool.shutdown(wait=True)
