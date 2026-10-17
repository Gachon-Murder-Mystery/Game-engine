"""External A/B/C engine contracts. No player policy, GM planner, or LLM calls.

Every mutation, receipt, pool update and role transition shares one SQLite
transaction. Legacy single-player and /multi demo states remain untouched.
"""
import copy
import hashlib
import json
import math
import random
import secrets
import sqlite3
import time
import threading
import uuid
from contextlib import closing, contextmanager

from engine import CAT, JOBS, PLACES, ROOMS

VERSION = 'engine-integration-v1'
PHASES = ('movement', 'interaction', 'deduction')
ACTIONS = {
    'movement': ('move', 'stay'),
    'interaction': ('explore', 'question', 'prepare', 'pass'),
    'deduction': ('accuse', 'pass'),
}
TRAITS = ('cooperative', 'cautious', 'independent', 'competitive')
GOALS = ('protect_secret', 'prove_innocence', 'steal_vault')
TACTICS = ('verify_before_accuse', 'ask_before_duplicate_search', 'share_when_stalled',
           'parallelize_independent_tasks', 'prefer_unexplored_rooms', 'request_before_escalation')
DEFAULTS = {'gm_lease': 40, 'player_lease': 40, 'question_timeout': 30,
            'exploration_rate': .2, 'max_failovers': 3,
            'min_players': 1, 'max_players': 12,
            'gm_visibility': 'ids_only', 'successor_scope': 'active_players'}


class ContractError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(condition, code, message):
    if not condition:
        raise ContractError(code, message)


def identifier(value, name):
    require(isinstance(value, str) and 0 < len(value) <= 100 and
            all(c.isalnum() or c in '-_:.' for c in value), 'invalid_input', f'{name}: invalid ID')
    return value


def integer(value, name, low=0, high=1000000):
    require(type(value) is int and low <= value <= high, 'invalid_input', f'{name}: invalid integer')
    return value


def text(value, name, maximum=1000):
    require(isinstance(value, str) and len(value) <= maximum, 'invalid_input', f'{name}: invalid text')
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def token_matches(value, expected):
    return isinstance(value, str) and bool(expected) and secrets.compare_digest(value, expected)


def catalog():
    return {'schema_version': VERSION, 'catalog_version': 'ornate-express-v1',
            'candidates': {k: [{'id': i, 'name': label} for i, label in enumerate(v)]
                           for k, v in CAT.items()},
            'places': [{'id': i, 'name': label} for i, label in enumerate(PLACES)],
            'jobs': [{'id': i, 'name': label, 'default_stat': 4} for i, label in enumerate(JOBS)],
            'personalities': list(TRAITS), 'personal_goals': list(GOALS),
            'phases': list(PHASES), 'allowed_actions': {phase: list(actions) for phase, actions in ACTIONS.items()},
            'strategy_tactics': list(TACTICS), 'default_rules': copy.deepcopy(DEFAULTS),
            'temporary_rules': ['1d6; prepared adds one die; stat=4 by default',
                                'wrong accusation consumes a phase but does not kill',
                                'partial disclosure masks exclusion; corpse rules deferred']}


class ExternalEngine:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = str(path), clock
        with self.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS games(id TEXT PRIMARY KEY, state TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS integration_config(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS agents(agent_id TEXT PRIMARY KEY, state TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS agent_learning(agent_id TEXT PRIMARY KEY, state TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS experience_events(event_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, state TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO integration_config VALUES (?,?)',
                       ('operator_token', secrets.token_urlsafe(32)))
            self.operator_token = db.execute('SELECT value FROM integration_config WHERE key=?',
                                             ('operator_token',)).fetchone()[0]

    @contextmanager
    def transaction(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db:
            try:
                db.execute('BEGIN IMMEDIATE')
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def load(db, table, key, column='agent_id'):
        # Table/column names are internal constants, never supplied by a caller.
        row = db.execute(f'SELECT state FROM {table} WHERE {column}=?', (key,)).fetchone()
        require(row is not None, 'not_found', f'{table}: record not found')
        return json.loads(row[0])

    @staticmethod
    def put(db, table, key, state, column='agent_id'):
        db.execute(f'INSERT OR REPLACE INTO {table} ({column},state) VALUES (?,?)',
                   (key, json.dumps(state, ensure_ascii=False, allow_nan=False)))

    def operator(self, d):
        require(token_matches(d.get('operator_token'), self.operator_token),
                'unauthorized', 'operator credential required')

    def member(self, db, d):
        a = self.load(db, 'agents', identifier(d.get('agent_id'), 'agent_id'))
        require(token_matches(d.get('pool_token'), a['pool_token']),
                'unauthorized', 'own pool credential required')
        return a

    def game(self, db, d):
        g = self.load(db, 'games', identifier(d.get('game_id'), 'game_id'), 'id')
        require(g.get('kind') == 'external', 'invalid_input', 'not an external integration game')
        return g

    def player(self, g, d, allow_ended=False):
        aid = identifier(d.get('agent_id'), 'agent_id')
        a = g['players'].get(aid)
        require(a is not None and a['mode'] == 'active' and
                token_matches(d.get('token'), a['token']), 'unauthorized', 'active player credential required')
        require(g['ended'] or g['status'] == 'paused' or self.clock() < a['lease_until'],
                'expired', 'player lease expired')
        require(allow_ended or (not g['ended'] and g['status'] == 'running'),
                'not_running', 'game is not running')
        return a

    def gm(self, g, d, allow_waiting=False, allow_ended=False):
        c = g['controller']
        require(c is not None and type(d.get('term')) is int and d['term'] == c['term'] and
                d.get('controller_id') == c['controller_id'] and
                token_matches(d.get('controller_token'), c['token']) and
                ((allow_ended and g['ended']) or self.clock() < c['lease_until']),
                'stale_controller', 'current GM term/token/lease required')
        require((allow_ended and g['ended']) or (not g['ended'] and
                (allow_waiting or g['status'] in ('running', 'awaiting_setup'))),
                'not_running', 'game is paused or ended')
        return c

    def event(self, g, kind, payload, visibility='shared', participants=()):
        e = {'schema_version': VERSION, 'game_id': g['id'], 'event_id': f"{g['id']}:event:{len(g['events'])+1}",
             'sequence': len(g['events'])+1, 'type': kind, 'timestamp': self.clock(),
             'visibility': visibility, 'participants': list(participants), 'payload': copy.deepcopy(payload)}
        g['events'].append(e)
        return e

    def experience(self, db, g, aid, source, role, outcome, success, tags=()):
        eid = f'{source}:experience:{aid}'
        e = {'schema_version': VERSION, 'event_id': eid, 'game_id': g['id'],
             'agent_id': aid, 'source_event_id': source, 'role': role,
             'outcome': outcome, 'feedback': {'success': bool(success)},
             'tags': list(tags), 'strategy_consumed': False}
        if db.execute('SELECT 1 FROM experience_events WHERE event_id=?', (eid,)).fetchone():
            return
        db.execute('INSERT INTO experience_events VALUES (?,?,?)', (eid, aid, json.dumps(e)))
        learning = self.load(db, 'agent_learning', aid)
        for tag in (role, *tags):
            score = learning['competencies'].setdefault(tag, {'attempts': 0, 'successes': 0})
            score['attempts'] += 1
            score['successes'] += int(bool(success))
            score['estimate'] = (score['successes']+1)/(score['attempts']+2)
        learning['feedback_version'] += 1
        self.put(db, 'agent_learning', aid, learning)

    def pool_cards(self, db):
        result = []
        for (raw,) in db.execute('SELECT state FROM agents ORDER BY agent_id'):
            a = json.loads(raw)
            l = self.load(db, 'agent_learning', a['agent_id'])
            result.append({k: a[k] for k in ('agent_id', 'model_version', 'pool_status', 'busy_game_id')} |
                          {'competencies': l['competencies'], 'learning_version': l['version']})
        return result

    def register(self, db, d):
        self.operator(d)
        aid = identifier(d.get('agent_id'), 'agent_id')
        require(not db.execute('SELECT 1 FROM agents WHERE agent_id=?', (aid,)).fetchone(),
                'conflict', 'persistent agent_id cannot be reused')
        a = {'agent_id': aid, 'model_version': text(d.get('model_version', 'unbound'), 'model_version', 100),
             'pool_status': 'available', 'busy_game_id': None, 'pool_token': secrets.token_urlsafe(32)}
        self.put(db, 'agents', aid, a)
        self.put(db, 'agent_learning', aid, {'version': 0, 'feedback_version': 0,
                 'strategy_state': {'tactics': [], 'weights': {}}, 'competencies': {}})
        return copy.deepcopy(a)

    def learning(self, db, d, update=False):
        a = self.member(db, d)
        aid = a['agent_id']
        l = self.load(db, 'agent_learning', aid)
        if update:
            require(d.get('expected_version') == l['version'] and type(d.get('expected_version')) is int,
                    'conflict', 'strategy version changed')
            ids = d.get('experience_event_ids')
            require(isinstance(ids, list) and 0 < len(ids) <= 100 and
                    all(isinstance(x, str) for x in ids) and len(ids) == len(set(ids)),
                    'invalid_input', 'unique official feedback IDs required')
            events = []
            for eid in ids:
                e = self.load(db, 'experience_events', eid, 'event_id')
                require(e['agent_id'] == aid and not e['strategy_consumed'],
                        'conflict', 'feedback belongs to another agent or was consumed')
                events.append(e)
            state = d.get('strategy_state')
            require(isinstance(state, dict) and set(state) == {'tactics', 'weights'},
                    'invalid_input', 'only generalized tactics and numeric weights are stored')
            require(isinstance(state['tactics'], list) and len(state['tactics']) <= len(TACTICS) and
                    all(x in TACTICS for x in state['tactics']), 'invalid_input', 'unknown strategy tactic')
            require(isinstance(state['weights'], dict) and
                    all(k in ('team', 'private', 'information', 'cost', 'risk') and
                        type(v) in (int, float) and math.isfinite(v) and -10 <= v <= 10
                        for k, v in state['weights'].items()), 'invalid_input', 'invalid strategy weights')
            l['strategy_state'] = copy.deepcopy(state)
            l['version'] += 1
            self.put(db, 'agent_learning', aid, l)
            for e in events:
                e['strategy_consumed'] = True
                db.execute('UPDATE experience_events SET state=? WHERE event_id=?',
                           (json.dumps(e), e['event_id']))
        feedback = [json.loads(r[0]) for r in db.execute(
            'SELECT state FROM experience_events WHERE agent_id=? ORDER BY rowid', (aid,))]
        return {'agent_id': aid, 'learning': l, 'experience_events': feedback}

    def session(self, db, d):
        """Bootstrap only the persistent worker's current engine-assigned role."""
        a = self.member(db, d)
        result = {'agent_id': a['agent_id'], 'pool_status': a['pool_status'],
                  'game_id': a['busy_game_id'], 'role': 'dormant', 'credentials': None}
        if a['busy_game_id'] is None:
            return result
        g = self.load(db, 'games', a['busy_game_id'], 'id')
        c = g['controller']
        if c and c['controller_id'] == a['agent_id'] and not g['ended'] and self.clock() < c['lease_until']:
            assignment = next(x for x in reversed(g['assignments']) if x['role'] == 'gm' and x['term'] == c['term'])
            result.update(role='gm', context_key=assignment['assignment_id'],
                          context_source='engine-gm-observation-only',
                          credentials={'game_id': g['id'], 'controller_id': c['controller_id'],
                                       'term': c['term'], 'controller_token': c['token']})
        else:
            p = g['players'].get(a['agent_id'])
            if p and p['mode'] == 'active' and not g['ended'] and self.clock() < p['lease_until']:
                result.update(role='player', context_key=p['assignment_id'], context_source='own-player-observation',
                              credentials={'game_id': g['id'], 'agent_id': a['agent_id'],
                                           'assignment_id': p['assignment_id'], 'token': p['token']})
            else:
                result['role'] = 'inactive'
        return result

    def select(self, db, candidates, seed, rate):
        ordered = sorted(candidates)
        rng = random.Random(seed)
        scores = {aid: self.load(db, 'agent_learning', aid)['competencies'].get('gm',
                    {'attempts': 0, 'estimate': .5}) for aid in ordered}
        explore = not any(s['attempts'] for s in scores.values()) or rng.random() < rate
        if explore:
            selected = rng.choice(ordered)
        else:
            maximum = max(s['estimate'] for s in scores.values())
            selected = rng.choice([aid for aid in ordered if scores[aid]['estimate'] == maximum])
        return selected, {'mode': 'exploration' if explore else 'competency', 'seed': seed,
                          'exploration_rate': rate, 'candidates': ordered,
                          'estimates': {aid: s['estimate'] for aid, s in scores.items()}}

    def reserve(self, db, aid, game_id):
        a = self.load(db, 'agents', aid)
        require(a['pool_status'] == 'available' and a['busy_game_id'] is None,
                'conflict', 'agent is unavailable or assigned to another game')
        a.update(pool_status='active', busy_game_id=game_id)
        self.put(db, 'agents', aid, a)

    def controller(self, g, aid):
        g['term'] += 1
        c = {'controller_id': aid, 'term': g['term'], 'token': secrets.token_urlsafe(32),
             'lease_until': self.clock()+g['rules']['gm_lease']}
        g['controller'] = c
        assignment = {'assignment_id': str(uuid.uuid4()), 'agent_id': aid, 'role': 'gm',
                      'term': c['term'], 'status': 'active'}
        g['assignments'].append(assignment)
        return c

    def create(self, db, d):
        self.operator(d)
        gid = identifier(d.get('game_id', str(uuid.uuid4())), 'game_id')
        require(not db.execute('SELECT 1 FROM games WHERE id=?', (gid,)).fetchone(),
                'conflict', 'game_id already exists')
        seed = integer(d.get('seed', 0), 'seed', 0, 2**32-1)
        rules = DEFAULTS | d.get('rules', {}) if isinstance(d.get('rules', {}), dict) else None
        require(rules is not None and set(rules) == set(DEFAULTS), 'invalid_input', 'unknown rule')
        for key in ('gm_lease', 'player_lease', 'question_timeout'):
            require(type(rules[key]) in (int, float) and math.isfinite(rules[key]) and 1 <= rules[key] <= 3600,
                    'invalid_input', f'{key}: invalid duration')
        integer(rules['max_failovers'], 'max_failovers', 1, 100)
        integer(rules['min_players'], 'min_players', 1, 12)
        integer(rules['max_players'], 'max_players', rules['min_players'], 12)
        require(type(rules['exploration_rate']) in (int, float) and math.isfinite(rules['exploration_rate']) and
                0 <= rules['exploration_rate'] <= 1, 'invalid_input', 'invalid exploration rate')
        require(rules['gm_visibility'] in ('ids_only', 'acquired_content') and
                rules['successor_scope'] in ('active_players', 'active_then_pool'),
                'invalid_input', 'invalid visibility or successor scope')
        eligible = [a['agent_id'] for a in self.pool_cards(db) if a['pool_status'] == 'available']
        candidates = d.get('candidate_ids', eligible)
        require(isinstance(candidates, list) and bool(candidates) and
                all(isinstance(x, str) and x in eligible for x in candidates) and
                len(candidates) == len(set(candidates)), 'invalid_input', 'eligible unique GM candidates required')
        aid, selection = self.select(db, candidates, seed, rules['exploration_rate'])
        self.reserve(db, aid, gid)
        rng = random.Random(seed)
        truth = {key: rng.randrange(len(values)) for key, values in CAT.items()}
        clues = []
        for key, values in CAT.items():
            for i, name in enumerate(values):
                if i != truth[key]:
                    clues.append({'id': 'clue:'+secrets.token_hex(12), 'category': key,
                                  'excluded_id': i, 'source_room': (len(clues)*3+2)%8,
                                  'text': f'공식 자료 대조: {name}은(는) {key} 후보에서 제외됩니다.'})
        g = {'id': gid, 'kind': 'external', 'schema_version': VERSION, 'seed': seed,
             'rules': rules, 'truth': truth, 'roll_secret': secrets.token_hex(24),
             'clues': clues, 'players': {}, 'shared': {},
             'assignments': [], 'tasks': {}, 'map_version': 0, 'plans': [], 'events': [],
             'receipts': {}, 'commands': {}, 'questions': {}, 'shares': {},
             'controller': None, 'term': 0, 'failovers': 0, 'episode_gms': [],
             'cycle_id': 1, 'phase': 'movement', 'phase_epoch': 1, 'spent': {},
             'status': 'awaiting_setup', 'ended': False, 'resolution': None,
             'selection': selection, 'created_at': self.clock(), 'finished_at': None}
        self.controller(g, aid)
        g['episode_gms'].append(aid)
        self.event(g, 'gm_selected', {'controller_id': aid, 'term': g['term'], 'mode': selection['mode']})
        self.put(db, 'games', gid, g, 'id')
        return self.credentials(g) | {'selection': selection, 'state': self.gm_view(g)}

    @staticmethod
    def credentials(g):
        c = g['controller']
        return {'game_id': g['id'], 'controller': ({'controller_id': c['controller_id'],
                'term': c['term'], 'controller_token': c['token']} if c else None),
                'agent_credentials': {aid: {'agent_id': aid, 'assignment_id': a['assignment_id'], 'token': a['token']}
                                      for aid, a in g['players'].items() if a['mode'] == 'active'}}

    @staticmethod
    def information(clue, partial=False):
        keys = ('id', 'category', 'text', 'excluded_id')
        if partial:
            return {'id': clue['id'], 'category': clue['category'], 'redacted': True,
                    'text': '관련 단서를 보유하고 있으나 구체적인 배제 내용은 공개하지 않았습니다.'}
        return {k: copy.deepcopy(clue[k]) for k in keys}

    def context(self, g):
        return {'schema_version': VERSION, 'game_id': g['id'], 'cycle_id': g['cycle_id'],
                'phase': g['phase'], 'phase_epoch': g['phase_epoch'], 'status': g['status'],
                'ended': g['ended'], 'resolution': g['resolution']}

    def gm_view(self, g):
        c = g['controller']
        result = self.context(g) | {
            'control': {'controller_id': c['controller_id'], 'term': c['term']} if c else None,
            'lease_until': c['lease_until'] if c else None,
            'active_agents': [aid for aid, a in g['players'].items() if a['mode'] == 'active'],
            'actors': [{'id': aid, 'character_id': a['character_id'], 'room': a['room'], 'mode': a['mode'],
                        'job': a['job'], 'personality': a['personality'],
                        'held_information_ids': list(a['information'])} for aid, a in g['players'].items()],
            'shared_information': list(g['shared'].values()), 'spent': copy.deepcopy(g['spent']),
            'event_sequence': len(g['events']), 'map_version': g['map_version'],
            'tasks': list(copy.deepcopy(g['tasks']).values()),
            'questions': [{k: q[k] for k in ('question_id', 'asker_id', 'target_id', 'status', 'deadline')}
                          for q in g['questions'].values()],
            'failovers': g['failovers'], 'plans': copy.deepcopy(g['plans']),
            'agent_map': self.agent_map(g), 'metrics': self.metrics(g),
            'rules': copy.deepcopy(g['rules']),
        }
        if g['rules']['gm_visibility'] == 'acquired_content':
            result['supervised_information'] = {aid: list(a['information'].values())
                                                for aid, a in g['players'].items()}
        return result

    @staticmethod
    def agent_map(g):
        nodes = [{'id': 'agent:'+aid, 'entity_id': aid, 'type': 'Agent', 'role': 'player' if a['mode'] == 'active' else 'inactive'}
                 for aid, a in g['players'].items()]
        if g['controller']:
            cid = g['controller']['controller_id']
            nodes = [x for x in nodes if x['id'] != 'agent:'+cid]
            nodes.append({'id': 'agent:'+cid, 'entity_id': cid, 'type': 'Agent', 'role': 'gm'})
        nodes += [{'id': a['character_id'], 'type': 'Character', 'mode': a['mode']} for a in g['players'].values()]
        nodes += [{'id': 'task:'+t['task_id'], 'entity_id': t['task_id'], 'type': 'Task', 'status': t['status']} for t in g['tasks'].values()]
        nodes += [{'id': f'location:{i}', 'type': 'Location'} for i in range(8)]
        known = {cid for a in g['players'].values() for cid in a['information']}
        nodes += [{'id': cid, 'type': 'Clue', 'shared': cid in g['shared']} for cid in sorted(known)]
        edges = [{'source': a['character_id'], 'relation': 'locatedAt', 'target': f"location:{a['room']}"}
                 for aid, a in g['players'].items()]
        edges += [{'source': 'agent:'+aid, 'relation': 'controls', 'target': a['character_id']}
                  for aid, a in g['players'].items() if a['mode'] == 'active']
        edges += [{'source': 'agent:'+aid if a['mode'] == 'active' else a['character_id'], 'relation': 'knows', 'target': cid}
                  for aid, a in g['players'].items() for cid in a['information']]
        for t in g['tasks'].values():
            if t['owner']:
                edges.append({'source': 'task:'+t['task_id'], 'relation': 'assignedTo', 'target': 'agent:'+t['owner']})
            edges += [{'source': 'task:'+t['task_id'], 'relation': 'dependsOn', 'target': 'task:'+dep} for dep in t['dependencies']]
        return {'version': g['map_version'], 'state_sequence': len(g['events']), 'nodes': nodes, 'edges': edges,
                'semantics': 'knows indicates inventory IDs, not permission to disclose'}

    def metrics(self, g):
        results = [r['result'] for r in g['receipts'].values() if r['result']['execution_status'] == 'completed']
        questions = list(g['questions'].values())
        answered = [q for q in questions if q['status'] in ('answered', 'refused', 'npc_policy')]
        return {'completed_actions': len(results), 'effective_actions': sum(
                    r['action'] not in ('pass', 'stay') and r['outcome'] in ('succeeded', 'solved') for r in results),
                'pending_questions': sum(q['status'] == 'pending' for q in questions),
                'question_response_rate': len(answered)/len(questions) if questions else None,
                'refused_questions': sum(q['status'] == 'refused' for q in questions),
                'timed_out_questions': sum(q['status'] == 'timed_out' for q in questions),
                'shared_clues': len(g['shared']), 'completed_tasks': sum(
                    t['status'] == 'completed' for t in g['tasks'].values()),
                'task_completion_rate': sum(t['status'] == 'completed' for t in g['tasks'].values())/len(g['tasks']) if g['tasks'] else None,
                'gm_failovers': g['failovers'], 'duration': (g['finished_at'] if g['finished_at'] is not None else self.clock())-g['created_at']}

    def player_view(self, g, a):
        own = {k: a[k] for k in ('assignment_id', 'character_id', 'room', 'job', 'stat',
                                'personality', 'personal_goal', 'prepared', 'mode')}
        own['id'] = a['agent_id']
        available = self.visible_information(g, a)
        excluded = {key: set() for key in CAT}
        for item in available.values():
            if 'excluded_id' in item:
                excluded[item['category']].add(item['excluded_id'])
        return self.context(g) | {'self': own, 'information': list(a['information'].values()),
                'shared_information': list(g['shared'].values()), 'candidates': catalog()['candidates'],
                'board': {key: [c | {'excluded': c['id'] in excluded[key]} for c in vals]
                          for key, vals in catalog()['candidates'].items()},
                'occupants': [{'id': aid, 'room': b['room'], 'mode': b['mode']}
                              for aid, b in g['players'].items() if aid != a['agent_id'] and b['room'] == a['room']],
                'positions': [{'id': aid, 'room': b['room'], 'mode': b['mode']} for aid, b in g['players'].items()],
                'allowed_actions': list(ACTIONS[g['phase']]) if g['status'] == 'running' and a['agent_id'] not in g['spent'] else [],
                'remaining_actions': int(g['status'] == 'running' and a['agent_id'] not in g['spent']),
                'lease_until': a['lease_until'],
                'tasks': [copy.deepcopy(t) for t in g['tasks'].values() if t['owner'] == a['agent_id']]}

    @staticmethod
    def visible_information(g, a):
        """Partial public copies must never overwrite already known full facts."""
        result = copy.deepcopy(g['shared'])
        for cid, info in a['information'].items():
            if cid not in result or 'excluded_id' in info or 'excluded_id' not in result[cid]:
                result[cid] = copy.deepcopy(info)
        return result

    def renew_player(self, g, a):
        a['lease_until'] = self.clock()+g['rules']['player_lease']

    def plan(self, db, g, d):
        require(type(d.get('map_version')) is int and d['map_version'] == g['map_version'],
                'conflict', 'agent map version changed')
        activation = d.get('activate', [])
        require(isinstance(activation, list) and len(activation) <= 12, 'invalid_input', 'invalid activation list')
        for item in activation:
            require(isinstance(item, dict), 'invalid_input', 'activation must be an object')
            aid = identifier(item.get('agent_id'), 'agent_id')
            require(aid != g['controller']['controller_id'] and aid not in g['players'],
                    'conflict', 'already assigned or current GM')
            require(g['status'] == 'awaiting_setup' or (g['phase'] == 'movement' and not g['spent']),
                    'conflict', 'activate only at setup or a fresh movement phase')
            self.reserve(db, aid, g['id'])
            job_id = integer(item.get('job_id', 0), 'job_id', 0, len(JOBS)-1)
            personality = item.get('personality', 'cooperative')
            goal = item.get('personal_goal', 'protect_secret')
            require(personality in TRAITS and goal in GOALS, 'invalid_input', 'unknown trait or goal')
            assignment = {'assignment_id': str(uuid.uuid4()), 'agent_id': aid,
                          'character_id': 'character:'+str(uuid.uuid4()), 'role': 'player',
                          'job': {'id': job_id, 'name': JOBS[job_id]}, 'personality': personality,
                          'status': 'active'}
            g['assignments'].append(copy.deepcopy(assignment))
            g['players'][aid] = assignment | {'personal_goal': goal, 'stat': integer(item.get('stat', 4), 'stat', 2, 5),
                'agent_id': aid, 'room': 0, 'prepared': False, 'mode': 'active', 'token': secrets.token_urlsafe(32),
                'information': {}, 'lease_until': self.clock()+g['rules']['player_lease']}
        active = [a for a in g['players'].values() if a['mode'] == 'active']
        minimum = g['rules']['min_players'] if g['status'] == 'awaiting_setup' else 1
        require(minimum <= len(active) <= g['rules']['max_players'],
                'invalid_input', 'active player count is outside configured limits')
        changes = d.get('tasks', [])
        require(isinstance(changes, list) and len(changes) <= 100, 'invalid_input', 'invalid tasks')
        changed_ids = set()
        for change in changes:
            require(isinstance(change, dict), 'invalid_input', 'task must be an object')
            tid = identifier(change.get('task_id'), 'task_id')
            require(tid not in changed_ids, 'invalid_input', 'duplicate task in one plan')
            changed_ids.add(tid)
            owner = change.get('agent_id')
            require(owner in g['players'] and g['players'][owner]['mode'] == 'active', 'invalid_input', 'task owner inactive')
            old = g['tasks'].get(tid)
            require(not old or old['status'] != 'completed', 'conflict', 'completed task cannot be reassigned')
            require(not old or (type(change.get('generation')) is int and change['generation'] == old['generation']),
                    'conflict', 'task generation changed')
            deps = change.get('dependencies', old['dependencies'] if old else [])
            require(isinstance(deps, list) and all(isinstance(x, str) for x in deps) and len(deps) == len(set(deps)),
                    'invalid_input', 'invalid dependency list')
            kind = change.get('kind', old['kind'] if old else 'explore')
            require(kind in ('move', 'explore', 'question', 'prepare', 'accuse', 'pass'), 'invalid_input', 'invalid task kind')
            g['tasks'][tid] = {'task_id': tid, 'owner': owner, 'generation': old['generation']+1 if old else 1,
                              'status': 'assigned', 'kind': kind, 'dependencies': deps,
                              'room': integer(change.get('room', old['room'] if old else 0), 'room', 0, 7)}
        visiting, visited = set(), set()
        def visit(tid):
            require(tid in g['tasks'] and tid not in visiting, 'invalid_input', 'missing dependency or task cycle')
            if tid in visited:
                return
            visiting.add(tid)
            for dep in g['tasks'][tid]['dependencies']:
                visit(dep)
            visiting.remove(tid)
            visited.add(tid)
        for tid in g['tasks']:
            visit(tid)
        for q in g['questions'].values():
            if q['status'] != 'pending':
                continue
            r = g['receipts'][q['action_id']]['result']
            task = g['tasks'].get(r['task_id'])
            if task and (task['owner'] != r['agent_id'] or task['generation'] != r['generation']):
                self.complete_question(db, g, q, [], True, 'full', 'stale_task')
        alternatives = d.get('alternatives', [])
        require(isinstance(alternatives, list) and len(alternatives) <= 10 and
                all(isinstance(x, dict) and set(x) <= {'id', 'summary', 'reason'} and
                    isinstance(x.get('id'), str) and isinstance(x.get('summary'), str) and
                    len(x['summary']) <= 1000 and isinstance(x.get('reason', ''), str) and
                    len(x.get('reason', '')) <= 1000 for x in alternatives), 'invalid_input', 'invalid alternatives')
        selected = d.get('selected_alternative')
        for alternative in alternatives:
            identifier(alternative['id'], 'alternative_id')
        require(len({x['id'] for x in alternatives}) == len(alternatives), 'invalid_input', 'duplicate alternative ID')
        require(selected is None or selected in [x['id'] for x in alternatives], 'invalid_input', 'selected alternative missing')
        refs = d.get('evidence_ids', [])
        require(isinstance(refs, list) and all(isinstance(x, str) and x in g['shared'] for x in refs),
                'unauthorized', 'GM plans may cite only shared evidence')
        g['map_version'] += 1
        g['plans'].append({'command_id': d['command_id'], 'controller_id': g['controller']['controller_id'],
                           'term': g['term'], 'map_version': g['map_version'],
                           'alternatives': copy.deepcopy(alternatives), 'selected_alternative': selected,
                           'reason': text(d.get('reason', ''), 'reason'), 'evidence_ids': refs})
        g['status'] = 'running'
        self.event(g, 'gm_plan_committed', {'map_version': g['map_version'], 'term': g['term'],
                                          'task_ids': [x['task_id'] for x in changes]})
        return {'map_version': g['map_version'], 'tasks': list(copy.deepcopy(g['tasks']).values())}

    def gm_command(self, db, g, d):
        c = self.gm(g, d, allow_ended=True)
        cid = identifier(d.get('command_id'), 'command_id')
        encoded = fingerprint(d)
        if cid in g['commands']:
            old = g['commands'][cid]
            require(old['fingerprint'] == encoded, 'conflict', 'command_id reused with different content')
            return old['result']
        require(not g['ended'], 'not_running', 'game ended')
        operation = d.get('operation')
        require(operation == 'FINISH' or g['resolution'] is None, 'not_running', 'official solution already recorded')
        if operation == 'PLAN':
            result = self.plan(db, g, d)
        elif operation == 'ADVANCE':
            require(type(d.get('phase_epoch')) is int and d['phase_epoch'] == g['phase_epoch'],
                    'conflict', 'phase epoch changed')
            active = [aid for aid, a in g['players'].items() if a['mode'] == 'active']
            require(active and all(aid in g['spent'] for aid in active) and
                    not any(q['status'] == 'pending' for q in g['questions'].values()),
                    'pending', 'wait for all actions and question responses')
            index = PHASES.index(g['phase'])
            g['phase'] = PHASES[(index+1)%3]
            g['cycle_id'] += int(index == 2)
            g['phase_epoch'] += 1
            g['spent'] = {}
            self.event(g, 'phase_advanced', {'phase': g['phase'], 'phase_epoch': g['phase_epoch']})
            result = self.context(g)
        elif operation == 'FINISH':
            require(g['resolution'] == 'solved', 'conflict', 'only an official solution can finish successfully')
            self.finish(db, g, 'solved')
            result = self.context(g)
        else:
            raise ContractError('invalid_input', 'unknown GM operation')
        if not g['ended']:
            c['lease_until'] = self.clock()+g['rules']['gm_lease']
        g['commands'][cid] = {'fingerprint': encoded, 'result': copy.deepcopy(result)}
        return result

    def action(self, db, g, d):
        a = self.player(g, d, allow_ended=True)
        request = d.get('request')
        require(isinstance(request, dict), 'invalid_input', 'request must be an object')
        require(set(request) <= {'schema_version', 'game_id', 'agent_id', 'action_id', 'cycle_id',
                                'phase', 'phase_epoch', 'action', 'payload', 'task_id', 'generation'},
                'invalid_input', 'unknown action request fields')
        require(request.get('schema_version', VERSION) == VERSION, 'unsupported_version', 'unsupported action schema version')
        require(request.get('game_id') == g['id'] and request.get('agent_id') == a['agent_id'],
                'unauthorized', 'action identity differs from credential')
        action_id = identifier(request.get('action_id'), 'action_id')
        encoded = fingerprint(request)
        if action_id in g['receipts']:
            old = g['receipts'][action_id]
            require(old['fingerprint'] == encoded, 'conflict', 'action_id reused with different content')
            return copy.deepcopy(old['result'])
        require(not g['ended'] and g['status'] == 'running' and g['resolution'] is None,
                'not_running', 'game is not running or already solved')
        require(all(type(request.get(key)) is int and request[key] == g[key] for key in ('cycle_id', 'phase_epoch')) and
                request.get('phase') == g['phase'], 'conflict', 'stale cycle or phase')
        require(a['agent_id'] not in g['spent'], 'conflict', 'active action already consumed')
        act = request.get('action')
        require(isinstance(act, str) and act in ACTIONS[g['phase']], 'invalid_input', 'action not allowed in phase')
        payload = request.get('payload', {})
        fields = {'move': {'room'}, 'stay': set(), 'explore': {'approach'},
                  'question': {'target_id', 'approach', 'topic'}, 'prepare': set(),
                  'accuse': set(CAT), 'pass': set()}[act]
        require(isinstance(payload, dict) and set(payload) <= fields, 'invalid_input', 'unknown action payload fields')
        tid = request.get('task_id')
        if tid is not None:
            identifier(tid, 'task_id')
            t = g['tasks'].get(tid)
            require(t is not None and t['owner'] == a['agent_id'] and t['status'] == 'assigned' and
                    type(request.get('generation')) is int and request['generation'] == t['generation'],
                    'conflict', 'task owner or generation changed')
            require(all(g['tasks'][dep]['status'] == 'completed' for dep in t['dependencies']),
                    'pending', 'task dependencies incomplete')
            require(t['kind'] == act and (act not in ('explore', 'question') or t['room'] == a['room']),
                    'invalid_input', 'action does not match assigned task')
            require(act != 'move' or payload.get('room') == t['room'],
                    'invalid_input', 'destination does not match assigned task')
        result = {k: copy.deepcopy(request[k]) for k in ('game_id', 'agent_id', 'action_id', 'cycle_id', 'phase', 'phase_epoch', 'action')}
        result.update(schema_version=VERSION, task_id=tid, generation=request.get('generation'),
                      execution_status='completed', consumed=True, outcome='succeeded',
                      acquired_information=[], roll=None, room=a['room'], actor_mode=a['mode'])
        if act == 'move':
            a['room'] = integer(payload.get('room'), 'room', 0, 7)
            result['room'] = a['room']
        elif act == 'prepare':
            a['prepared'] = True
        elif act == 'accuse':
            guess = {key: integer(payload.get(key), key, 0, len(CAT[key])-1) for key in CAT}
            won = guess == g['truth']
            result['outcome'] = 'solved' if won else 'wrong_accusation'
            if won:
                g['resolution'] = 'solved'
        elif act in ('explore', 'question'):
            approach = payload.get('approach', 'compassion')
            require(approach in ('compassion', 'brutality'), 'invalid_input', 'invalid approach')
            rng = random.Random(f"{g['roll_secret']}:{a['agent_id']}:{action_id}")
            dice = [rng.randint(1, 6) for _ in range(2 if a['prepared'] else 1)]
            a['prepared'] = False
            successes = sum(v > a['stat'] if approach == 'compassion' else v < a['stat'] for v in dice)
            result['roll'] = {'dice': dice, 'approach': approach, 'successes': successes}
            if act == 'explore':
                known = self.visible_information(g, a)
                pool = [clue for clue in g['clues'] if clue['source_room'] == a['room'] and
                        ('excluded_id' not in known.get(clue['id'], {}))]
                gained = pool[:int(successes > 0)]
                result['acquired_information'] = [self.information(clue) for clue in gained]
                result['outcome'] = 'succeeded' if successes else 'failed'
                a['information'].update({x['id']: x for x in result['acquired_information']})
            else:
                target = g['players'].get(payload.get('target_id'))
                require(target is not None and target['agent_id'] != a['agent_id'] and
                        target['room'] == a['room'], 'invalid_input', 'question target must be another actor in the same room')
                qid = 'question:'+str(uuid.uuid4())
                q = {'question_id': qid, 'conversation_id': qid, 'game_id': g['id'],
                     'asker_id': a['agent_id'], 'target_id': target['agent_id'],
                     'sender_agent_id': a['agent_id'], 'recipient_agent_id': target['agent_id'],
                     'performative': 'request', 'topic': text(payload.get('topic', ''), 'topic', 200),
                     'visibility': 'private', 'reply_to': None, 'status': 'pending',
                     'deadline': self.clock()+g['rules']['question_timeout'], 'action_id': action_id,
                     'response_id': None, 'response_fingerprint': None}
                g['questions'][qid] = q
                result.update(execution_status='pending', outcome='awaiting_response', question_id=qid,
                              target_id=target['agent_id'])
                if target['mode'] != 'active':
                    # NPC passive policy is an engine rule, not an agent decision.
                    offers = [cid for cid, clue in target['information'].items() if 'excluded_id' in clue]
                    self.event(g, 'question_requested', self.question_summary(q), 'supervisor', (a['agent_id'], target['agent_id']))
        g['spent'][a['agent_id']] = action_id
        self.renew_player(g, a)
        result['held_information_ids'] = list(a['information'])
        result['resolution'] = g['resolution']
        g['receipts'][action_id] = {'fingerprint': encoded, 'request': copy.deepcopy(request), 'result': result}
        if result['execution_status'] == 'pending':
            q = g['questions'][result['question_id']]
            if g['players'][q['target_id']]['mode'] == 'active':
                self.event(g, 'question_requested', self.question_summary(q), 'supervisor', (a['agent_id'], q['target_id']))
            else:
                self.complete_question(db, g, q, offers, False, 'full', 'npc_policy')
        else:
            self.complete_action(db, g, result)
        return copy.deepcopy(g['receipts'][action_id]['result'])

    @staticmethod
    def question_summary(q):
        return {k: q[k] for k in ('question_id', 'conversation_id', 'asker_id', 'target_id', 'status', 'action_id', 'deadline')}

    def complete_action(self, db, g, result):
        a = g['players'][result['agent_id']]
        tid = result['task_id']
        if tid and tid in g['tasks']:
            t = g['tasks'][tid]
            # Failure is a completed attempt, not successful completion of the task.
            if t['owner'] == a['agent_id'] and t['generation'] == result['generation'] and result['outcome'] in ('succeeded', 'solved'):
                t['status'] = 'completed'
        event = self.event(g, 'action_completed', {'result': copy.deepcopy(result)}, 'private', (a['agent_id'],))
        self.experience(db, g, a['agent_id'], event['event_id'], 'player', result['outcome'],
                        result['outcome'] in ('succeeded', 'solved'),
                        (f"job:{a['job']['id']}", f"personality:{a['personality']}"))

    def complete_question(self, db, g, q, offers, refused, disclosure, outcome=None):
        a = g['players'][q['asker_id']]
        target = g['players'][q['target_id']]
        r = g['receipts'][q['action_id']]['result']
        q['status'] = outcome or ('refused' if refused else 'answered')
        q['performative'] = 'refuse' if refused else 'inform'
        r.update(execution_status='completed', outcome=outcome or ('refused' if refused else 'succeeded'),
                 acquired_information=[])
        if not refused and outcome in (None, 'npc_policy') and r['roll']['successes']:
            known = self.visible_information(g, a)
            selected = next((cid for cid in offers if cid not in known or
                             (disclosure == 'full' and 'excluded_id' in target['information'][cid]
                              and 'excluded_id' not in known[cid])), None)
            if selected:
                clue = target['information'][selected]
                # A partially known clue cannot be upgraded to a full official fact.
                info = self.information(clue, disclosure == 'partial' or 'excluded_id' not in clue)
                a['information'][selected] = info
                r['acquired_information'] = [info]
        if not refused and outcome in (None, 'npc_policy'):
            r['response_source'] = 'npc_policy' if outcome == 'npc_policy' else 'agent'
            r['outcome'] = ('succeeded' if r['acquired_information'] else
                            'failed' if not r['roll']['successes'] else 'no_new_information')
        r['held_information_ids'] = list(a['information'])
        r['resolution'] = g['resolution']
        self.complete_action(db, g, r)
        self.event(g, 'question_completed', self.question_summary(q), 'supervisor', (q['asker_id'], q['target_id']))

    def respond(self, db, g, d):
        a = self.player(g, d, allow_ended=True)
        qid = identifier(d.get('question_id'), 'question_id')
        q = g['questions'].get(qid)
        require(q is not None and q['target_id'] == a['agent_id'], 'unauthorized', 'question belongs to another receiver')
        response_id = identifier(d.get('response_id'), 'response_id')
        encoded = fingerprint(d)
        if q['response_id'] is not None:
            require(q['response_id'] == response_id and q['response_fingerprint'] == encoded,
                    'conflict', 'response already committed or ID reused')
            return self.question_summary(q)
        require(not g['ended'] and g['status'] == 'running', 'not_running', 'game is not running')
        require(q['status'] == 'pending' and self.clock() < q['deadline'], 'expired', 'question no longer pending')
        offers, refused = d.get('offer_ids'), d.get('refused')
        require(type(refused) is bool and isinstance(offers, list) and
                all(isinstance(x, str) and x in a['information'] for x in offers) and len(offers) == len(set(offers)),
                'unauthorized', 'offer only owned evidence IDs; refused must be a boolean')
        require(not refused or not offers, 'invalid_input', 'refusal cannot include offers')
        disclosure = d.get('disclosure', 'full')
        require(disclosure in ('full', 'partial'), 'invalid_input', 'invalid disclosure')
        q.update(response_id=response_id, response_fingerprint=encoded, reply_to=qid)
        self.renew_player(g, a)
        self.complete_question(db, g, q, offers, refused, disclosure)
        return self.question_summary(q)

    def share(self, db, g, d):
        a = self.player(g, d, allow_ended=True)
        mid = identifier(d.get('message_id'), 'message_id')
        encoded = fingerprint(d)
        if mid in g['shares']:
            old = g['shares'][mid]
            require(old['fingerprint'] == encoded, 'conflict', 'message_id reused')
            return old['result']
        require(not g['ended'] and g['status'] == 'running', 'not_running', 'game is not running')
        ids = d.get('information_ids')
        require(isinstance(ids, list) and bool(ids) and len(ids) == len(set(ids)) and
                all(isinstance(x, str) and x in a['information'] for x in ids),
                'unauthorized', 'share only owned information')
        disclosure = d.get('disclosure', 'full')
        require(disclosure in ('full', 'partial'), 'invalid_input', 'invalid disclosure')
        published = []
        for cid in ids:
            clue = a['information'][cid]
            info = self.information(clue, disclosure == 'partial' or 'excluded_id' not in clue)
            if cid not in g['shared'] or 'excluded_id' not in g['shared'][cid]:
                g['shared'][cid] = info
            published.append(copy.deepcopy(g['shared'][cid]))
        e = self.event(g, 'information_shared', {'message_id': mid, 'sender_agent_id': a['agent_id'],
                       'performative': 'inform', 'visibility': 'shared', 'information': published})
        result = {'message_id': mid, 'event_id': e['event_id'], 'shared_information': published, 'consumed': False}
        g['shares'][mid] = {'fingerprint': encoded, 'result': result}
        self.renew_player(g, a)
        return result

    def fail_player(self, db, g, aid, reason):
        a = g['players'][aid]
        if a['mode'] != 'active':
            return
        a.update(mode='npc', token='', lease_until=0)
        for assignment in g['assignments']:
            if assignment['assignment_id'] == a['assignment_id']:
                assignment['status'] = 'npc'
        for t in g['tasks'].values():
            if t['owner'] == aid and t['status'] != 'completed':
                t.update(owner=None, status='pending', generation=t['generation']+1)
        g['map_version'] += 1
        for q in g['questions'].values():
            if q['status'] == 'pending' and aid in (q['asker_id'], q['target_id']):
                self.complete_question(db, g, q, [], True, 'full', 'cancelled')
        p = self.load(db, 'agents', aid)
        p['pool_status'] = 'fault'
        self.put(db, 'agents', aid, p)
        self.event(g, 'player_fault', {'agent_id': aid, 'reason': reason, 'character_mode': 'npc'})
        if not any(a['mode'] == 'active' for a in g['players'].values()):
            g['status'] = 'waiting_for_human'

    def failover(self, db, g, reason, chosen=None):
        old = g['controller']
        if old:
            old_aid = old['controller_id']
            for assignment in g['assignments']:
                if assignment['role'] == 'gm' and assignment['term'] == old['term']:
                    assignment['status'] = 'retired'
            p = self.load(db, 'agents', old_aid)
            p['pool_status'] = 'fault'
            self.put(db, 'agents', old_aid, p)
            e = self.event(g, 'gm_retired', {'controller_id': old_aid, 'term': old['term'], 'reason': reason})
            self.experience(db, g, old_aid, e['event_id'], 'gm', 'gm_fault', False)
        g['controller'] = None  # Fence first, even when no eligible successor exists.
        g['failovers'] += 1
        active = sorted(aid for aid, a in g['players'].items()
                        if a['mode'] == 'active' and self.clock() < a['lease_until'])
        reserve = [a['agent_id'] for a in self.pool_cards(db) if a['pool_status'] == 'available']
        candidates = active or (reserve if g['rules']['successor_scope'] == 'active_then_pool' else [])
        if chosen is not None:
            require(chosen in active or chosen in reserve, 'invalid_input', 'operator successor is not eligible')
            candidates = [chosen]
        if not candidates or (chosen is None and g['failovers'] > g['rules']['max_failovers']):
            g['status'] = 'waiting_for_human'
            self.event(g, 'human_intervention_required', {'reason': 'no_successor_or_failover_limit',
                                                         'choices': ['appoint_gm', 'finish']})
            return
        aid, selection = self.select(db, candidates, g['seed']+g['term'], g['rules']['exploration_rate'])
        if aid in active:
            self.fail_player(db, g, aid, 'promoted_to_gm')
            p = self.load(db, 'agents', aid)
            p['pool_status'] = 'active'
            self.put(db, 'agents', aid, p)
        else:
            self.reserve(db, aid, g['id'])
        self.controller(g, aid)
        g['episode_gms'].append(aid)
        g['status'] = 'running' if any(a['mode'] == 'active' for a in g['players'].values()) else 'awaiting_setup'
        self.event(g, 'gm_succeeded', {'controller_id': aid, 'term': g['term'], 'mode': selection['mode']})

    def finish(self, db, g, reason):
        require(not g['ended'], 'conflict', 'game already ended')
        for q in g['questions'].values():
            if q['status'] == 'pending':
                self.complete_question(db, g, q, [], True, 'full', 'cancelled')
        g.update(ended=True, status='ended', resolution=reason, finished_at=self.clock())
        e = self.event(g, 'game_finished', {'reason': reason})
        if g['controller']:
            self.experience(db, g, g['controller']['controller_id'], e['event_id'], 'gm', reason, reason == 'solved')
        for assignment in g['assignments']:
            if assignment['status'] == 'active':
                assignment['status'] = 'completed'
        for aid in set(a['agent_id'] for a in g['assignments']):
            p = self.load(db, 'agents', aid)
            if p['busy_game_id'] == g['id']:
                p['busy_game_id'] = None
                if p['pool_status'] == 'active':
                    p['pool_status'] = 'available'
                self.put(db, 'agents', aid, p)
        # Archive credentials can read their own final view; mutations reject ended games.

    def operator_command(self, db, g, d):
        self.operator(d)
        op = d.get('operation')
        require(not g['ended'], 'conflict', 'game already ended')
        if op == 'PAUSE':
            require(g['status'] != 'paused', 'conflict', 'already paused')
            g['paused_at'] = self.clock()
            g['status'] = 'paused'
        elif op == 'RESUME':
            require(g['status'] == 'paused' and g['controller'] is not None and
                    any(a['mode'] == 'active' for a in g['players'].values()), 'conflict', 'appoint GM or activate players first')
            g['controller']['lease_until'] = self.clock()+g['rules']['gm_lease']
            delay = self.clock()-g.pop('paused_at', self.clock())
            for q in g['questions'].values():
                if q['status'] == 'pending':
                    q['deadline'] += delay
            g['status'] = 'running'
            for a in g['players'].values():
                if a['mode'] == 'active':
                    self.renew_player(g, a)
        elif op == 'APPOINT_GM':
            self.failover(db, g, 'operator_appointment', identifier(d.get('agent_id'), 'agent_id'))
        elif op == 'FAIL_PLAYER':
            aid = identifier(d.get('agent_id'), 'agent_id')
            require(aid in g['players'], 'not_found', 'player not found')
            self.fail_player(db, g, aid, 'operator_injected_failure')
        elif op == 'FAIL_GM':
            require(type(d.get('term')) is int and g['controller'] and d['term'] == g['controller']['term'],
                    'conflict', 'specify current GM term')
            self.failover(db, g, 'operator_injected_failure')
        elif op == 'FINISH':
            self.finish(db, g, 'operator_stopped')
        else:
            raise ContractError('invalid_input', 'unknown operator operation')
        self.event(g, 'operator_action', {'operation': op, 'agent_id': d.get('agent_id')})
        return self.credentials(g) | {'state': self.gm_view(g)}

    def filtered_events(self, g, after, role, aid=None):
        integer(after, 'after')
        result = []
        for e in g['events']:
            if e['sequence'] <= after:
                continue
            if role == 'player' and e['visibility'] != 'shared' and aid not in e['participants']:
                continue
            event = copy.deepcopy(e)
            if e['type'] == 'action_completed' and (role == 'gm' or aid != e['payload']['result']['agent_id']):
                r = event['payload']['result']
                acquired = r.pop('acquired_information', [])
                r['acquired_information_ids'] = [c['id'] for c in acquired]
                if role == 'gm' and g['rules']['gm_visibility'] == 'acquired_content':
                    r['acquired_information'] = acquired
            result.append(event)
        return {'events': result, 'event_sequence': len(g['events']),
                'cursor_note': 'player-visible sequences can have gaps; advance to event_sequence'}

    def sweep(self, db, g):
        if g['ended'] or g['status'] == 'paused':
            return
        now = self.clock()
        for q in g['questions'].values():
            if q['status'] == 'pending' and now >= q['deadline']:
                self.complete_question(db, g, q, [], True, 'full', 'timed_out')
        # Expired players cannot remain apparently eligible when a GM expires too.
        for aid, a in g['players'].items():
            if a['mode'] == 'active' and now >= a['lease_until']:
                self.fail_player(db, g, aid, 'lease_expired')
        if g['controller'] and now >= g['controller']['lease_until']:
            self.failover(db, g, 'lease_expired')

    def tick(self):
        with self.transaction() as db:
            rows = list(db.execute('SELECT id,state FROM games'))
            for gid, raw in rows:
                g = json.loads(raw)
                if g.get('kind') == 'external' and not g['ended']:
                    self.sweep(db, g)
                    self.put(db, 'games', gid, g, 'id')

    def handle(self, route, d):
        require(isinstance(d, dict), 'invalid_input', 'JSON object required')
        require(d.get('schema_version', VERSION) == VERSION, 'unsupported_version', 'unsupported schema version')
        with self.transaction() as db:
            if route == 'catalog':
                return catalog()
            if route == 'pool/register':
                return self.register(db, d)
            if route == 'pool/list':
                self.operator(d)
                return {'agents': self.pool_cards(db)}
            if route == 'pool/credentials':
                self.operator(d)
                a = self.load(db, 'agents', identifier(d.get('agent_id'), 'agent_id'))
                return {'agent_id': a['agent_id'], 'pool_token': a['pool_token']}
            if route == 'pool/status':
                self.operator(d)
                a = self.load(db, 'agents', identifier(d.get('agent_id'), 'agent_id'))
                require(a['busy_game_id'] is None, 'conflict', 'end the assigned game before changing pool status')
                require(d.get('pool_status') in ('available', 'dormant', 'fault'), 'invalid_input', 'invalid pool status')
                a['pool_status'] = d['pool_status']
                self.put(db, 'agents', a['agent_id'], a)
                return {k: a[k] for k in ('agent_id', 'pool_status')}
            if route in ('pool/learning/get', 'pool/learning/update'):
                return self.learning(db, d, route.endswith('/update'))
            if route == 'pool/session':
                return self.session(db, d)
            if route == 'games/create':
                return self.create(db, d)
            if route == 'games/list':
                self.operator(d)
                games = [json.loads(raw) for (raw,) in db.execute('SELECT state FROM games')]
                return {'games': [self.context(g) | {'control': self.gm_view(g)['control']}
                                  for g in games if g.get('kind') == 'external']}
            g = self.game(db, d)
            # Expiration is committed by tick(), not hidden in a failing request transaction.
            if route == 'games/credentials':
                self.operator(d)
                result = self.credentials(g)
            elif route == 'games/observe':
                self.operator(d)
                result = self.gm_view(g) | {'assignments': copy.deepcopy(g['assignments']),
                          'operator_choices': ['APPOINT_GM', 'FINISH'] if g['status'] == 'waiting_for_human' else ['PAUSE', 'FINISH']}
            elif route == 'games/operator':
                result = self.operator_command(db, g, d)
            elif route == 'gm/observe':
                self.gm(g, d, allow_waiting=True, allow_ended=True)
                result = self.gm_view(g) | {'pool': self.pool_cards(db)}
            elif route == 'gm/heartbeat':
                c = self.gm(g, d, allow_waiting=True)
                c['lease_until'] = self.clock()+g['rules']['gm_lease']
                result = {'lease_until': c['lease_until'], 'term': c['term']}
            elif route == 'gm/command':
                result = self.gm_command(db, g, d)
            elif route in ('agent/observe', 'agent/heartbeat'):
                a = self.player(g, d, allow_ended=True)
                if route.endswith('heartbeat'):
                    require(not g['ended'], 'not_running', 'game ended')
                    self.renew_player(g, a)
                result = self.player_view(g, a)
            elif route == 'agent/action':
                result = self.action(db, g, d)
            elif route == 'agent/result':
                a = self.player(g, d, allow_ended=True)
                r = g['receipts'].get(d.get('action_id'))
                require(r is not None and r['result']['agent_id'] == a['agent_id'], 'unauthorized', 'own receipt required')
                result = copy.deepcopy(r['result'])
            elif route == 'agent/share':
                result = self.share(db, g, d)
            elif route == 'questions/inbox':
                a = self.player(g, d)
                result = {'questions': [copy.deepcopy(q) for q in g['questions'].values()
                         if q['target_id'] == a['agent_id'] and q['status'] == 'pending' and self.clock() < q['deadline']]}
            elif route == 'questions/respond':
                result = self.respond(db, g, d)
            elif route == 'events':
                if 'controller_token' in d:
                    self.gm(g, d, allow_waiting=True, allow_ended=True)
                    result = self.filtered_events(g, d.get('after', 0), 'gm')
                else:
                    a = self.player(g, d, allow_ended=True)
                    result = self.filtered_events(g, d.get('after', 0), 'player', a['agent_id'])
            elif route == 'documents/search':
                a = self.player(g, d)
                query = text(d.get('query', ''), 'query', 200)
                info = self.visible_information(g, a)
                result = {'documents': [copy.deepcopy(x) for x in info.values() if query in x['text']],
                          'location_description': ROOMS[a['room']], 'source': 'official-acquired-case-records'}
                self.event(g, 'document_search', {'agent_id': a['agent_id'], 'result_ids': [x['id'] for x in result['documents']]},
                           'supervisor', (a['agent_id'],))
            else:
                raise ContractError('not_found', 'unknown integration route')
            self.put(db, 'games', g['id'], g, 'id')
            return copy.deepcopy(result)


class ExternalWatchdog:
    """Engine-owned timer. Never calls an external agent or makes a task plan."""
    def __init__(self, engine, interval=.2):
        self.engine, self.interval = engine, interval
        self.stop = threading.Event()
        self.last_error = None
        self.thread = threading.Thread(target=self.run, name='external-engine-watchdog', daemon=True)

    def start(self):
        self.engine.tick()
        self.thread.start()

    def run(self):
        while not self.stop.wait(self.interval):
            try:
                self.engine.tick()
                self.last_error = None
            except Exception as exc:
                self.last_error = type(exc).__name__

    def close(self):
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=10)
