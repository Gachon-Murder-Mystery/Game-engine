"""Spec v1.3 data, truthful inference and mission predicates (no agent policy)."""
import json
import random
from pathlib import Path

DATA = json.loads(Path(__file__).with_name('competitive_catalog.json').read_text(encoding='utf-8'))
MBTI = DATA['mbti']
TIERS = ['하', '중', '상']
REWARDS = {'하': 10, '중': 20, '상': 30}
SHEETS = {c['id']: c for c in DATA['characters']}
MISSIONS = {m['id']: m for m in DATA['missions']}


def tier(mission_id, character_id, mbti, n=None):
    m, c = MISSIONS[mission_id], SHEETS[character_id]
    base = m.get('quantity', {}).get(str(n)) if m.get('quantity') else m['base']
    if base not in TIERS:
        return None
    def matches(t):
        if t in DATA['stat_tokens']:
            return c['stats'][DATA['stat_tokens'][t]] == 5
        return t == c['job'] or (len(t) == 1 and t in mbti)
    easy, hard = any(map(matches, m['easier'])), any(map(matches, m['harder']))
    return TIERS[max(0, min(2, TIERS.index(base)+(int(hard)-int(easy))))]


def candidates(seed, aid, cid, mbti, previous=(), player_count=3):
    result = {}
    for t in TIERS:
        ids = []
        for mid, m in MISSIONS.items():
            if mid in previous or (m['designate'] == 'npc' and player_count == 12):
                continue
            quantities = [int(x) for x in m['quantity']] if m.get('quantity') else [None]
            if m.get('quantity'):
                eligible = sorted({tier(mid, cid, mbti, n) for n in quantities})
                assigned_slot = random.Random(f'quantity-slot:{seed}:{aid}:{cid}:{mid}').choice(eligible)
                if t != assigned_slot: continue
            if any(tier(mid, cid, mbti, n) == t for n in quantities):
                ids.append(mid)
        rng = random.Random(f'missions:{seed}:{aid}:{cid}:{t}:{player_count}')
        result[t] = rng.sample(sorted(ids), min(3, len(ids)))
    return result


def infer(information):
    """Close only accessible facts. No truth lookup; supports are clue ID sets."""
    excluded = {k: {} for k in ('suspect', 'weapon', 'place')}
    positions = {'suspect': {}, 'weapon': {}}
    companion = []
    for item in information:
        cid = item['id']
        if item.get('kind') == 'definite':
            excluded[item['category']][item['candidate']] = {cid}
        elif item.get('kind') == 'indirect':
            if item['type'] == 'companion':
                companion.append((item['character'], item['other'], {cid}))
            else:
                cat, key = ('suspect', 'character') if item['type'] == 'character_room' else ('weapon', 'weapon')
                positions[cat][item[key]] = (item['room'], {cid})
    changed = True
    while changed:
        changed = False
        for a, b, support in companion:
            for x, y in ((a, b), (b, a)):
                if x not in positions['suspect'] and y in positions['suspect']:
                    room, basis = positions['suspect'][y]
                    positions['suspect'][x] = (room, support | basis)
                    changed = True
        scene = [i for i in range(8) if i not in excluded['place']]
        for cat in ('suspect', 'weapon'):
            for i, (room, support) in positions[cat].items():
                basis = excluded['place'].get(room)
                if basis is None and len(scene) == 1 and room != scene[0]:
                    basis = set().union(*excluded['place'].values())
                if basis is not None and i not in excluded[cat]:
                    excluded[cat][i] = basis | support
                    changed = True
            remaining = [i for i in range(12 if cat == 'suspect' else 8) if i not in excluded[cat]]
            if len(remaining) == 1 and remaining[0] in positions[cat]:
                room, support = positions[cat][remaining[0]]
                support = support | set().union(*excluded[cat].values())
                for i in range(8):
                    if i != room and i not in excluded['place']:
                        excluded['place'][i] = support
                        changed = True
    return {cat: {str(i): sorted(support) for i, support in values.items()} for cat, values in excluded.items()}


def narrative_options(clue):
    """Closed, truth-preserving grammar. Unverifiable arbitrary prose is rejected."""
    if clue.get('kind') != 'indirect':
        return []
    if clue['type'] == 'character_room':
        name, room = SHEETS[clue['character']]['name'], DATA['places'][clue['room']]
        return [f'자정에 {room}에 누군가 있었어요.', f'자정쯤 {room}에 누가 있었던 건 확실해요.', f'{name}은(는) 자정에 어느 칸에 있었던 것 같아요.',
                f'{name}은(는) 자정에 {room}에 있었던 것 같아요.']
    if clue['type'] == 'companion':
        a, b = SHEETS[clue['character']]['name'], SHEETS[clue['other']]['name']
        return [f'{a}은(는) 자정에 혼자가 아니었어요.', f'{a} 씨는 그날 밤 혼자가 아니었어요.', f'{a}은(는) 자정에 {b}와 함께 있었던 것 같아요.']
    return [f'어떤 흉기가 자정에 {DATA["places"][clue["room"]]}에 있었던 것 같아요.',
            f'그 시간 {DATA["places"][clue["room"]]} 어딘가에 흉기가 하나 있었다고 들었어요.',
            f'{clue["weapon_name"]}은(는) 자정에 어느 칸에 있었어요.']


def build_case(seed):
    rng = random.Random(f'case:{seed}')
    truth = {'suspect': rng.randrange(12), 'weapon': rng.randrange(8), 'place': rng.randrange(8)}
    chars, weapons = [rng.randrange(8) for _ in range(12)], [rng.randrange(8) for _ in range(8)]
    chars[truth['suspect']] = weapons[truth['weapon']] = truth['place']
    clues = []
    def add(data):
        i = len(clues)
        # Opaque to recipients: no category/candidate index in the ID.
        import hashlib
        cid = 'clue:'+hashlib.sha256(f'case-clue:{seed}:{i}'.encode()).hexdigest()[:24]
        clues.append({'id': cid, 'source_room': (i*3+2)%8, **data})
    for cat, size in (('suspect', 12), ('weapon', 8), ('place', 8)):
        for i in range(size):
            if i != truth[cat]:
                add({'kind': 'definite', 'category': cat, 'candidate': i, 'excluded_id': i,
                     'text': f'{cat} 후보 {i}은(는) 정답이 아니다.'})
    for i, room in enumerate(chars):
        add({'kind': 'indirect', 'type': 'character_room', 'character': i, 'room': room,
             'text': f'{SHEETS[i]["name"]}은(는) 자정에 {DATA["places"][room]}에 있었다.'})
    from engine import WEAPONS
    for i, room in enumerate(weapons):
        add({'kind': 'indirect', 'type': 'weapon_room', 'weapon': i, 'weapon_name': WEAPONS[i], 'room': room,
             'text': f'{WEAPONS[i]}은(는) 자정에 {DATA["places"][room]}에 있었다.'})
    for room in range(8):
        people = [i for i, r in enumerate(chars) if r == room]
        for a, b in zip(people, people[1:]):
            add({'kind': 'indirect', 'type': 'companion', 'character': a, 'other': b,
                 'text': f'{SHEETS[a]["name"]}은(는) 자정에 {SHEETS[b]["name"]}와 같이 있었다.'})
    closure = infer(clues)
    assert all(len(closure[k]) == size-1 and str(truth[k]) not in closure[k]
               for k, size in (('suspect', 12), ('weapon', 8), ('place', 8)))
    return truth, {'character_rooms': chars, 'weapon_rooms': weapons}, clues


def mission_complete(g, aid, mission):
    """All 58 mission predicates. Only engine facts/counters, never case truth."""
    p = g['players'][aid]
    cid = p['character_id']
    c = g['characters'][str(cid)]
    mid, targets = mission['mission_id'], mission.get('targets', {})
    acts = [x for x in g['activity'] if x['actor'] == cid]
    cycles = [x for x in g['cycle_history'] if str(cid) in x['alive']]
    current = g['cycle_id']
    qs = [x for x in acts if x['kind'] == 'question']
    search = [x for x in acts if x['kind'] == 'explore']
    success = [x for x in acts if x['successes'] > 0]
    agreements = [x for x in g['trades'].values() if aid in (x['proposer'], x['recipient']) and x['accepted']]
    trades = [x for x in agreements if x['status'] == 'settled']
    incoming = [v[str(cid)] for owner, v in g['relationships'].items() if int(owner) != cid]
    npc_incoming = [g['relationships'][str(i)][str(cid)] for i in range(12)
                    if i != cid and g['characters'][str(i)]['mode'] == 'npc']
    target_cid = targets.get('character')
    if 'player' in targets and targets['player'] in g['players']:
        target_cid = g['players'][targets['player']]['character_id']
    relation = g['relationships'].get(str(target_cid), {}).get(str(cid), -1)
    def at(room): return sum(x['rooms'][str(cid)] == room for x in cycles)
    def streak(room, extra=lambda x: True):
        n = best = 0
        for x in cycles:
            n = n+1 if x['rooms'][str(cid)] == room and extra(x) else 0
            best = max(best, n)
        return best
    def received(frame=None):
        return [x for x in g['activity'] if x.get('target') == cid and x['kind'] == 'question'
                and (frame is None or x['cycle'] == frame)]
    def duration(n, predicate):
        return len(cycles) >= n and all(predicate(x) for x in acts if x['cycle'] <= n)
    definite = [x for x in c['information'].values() if x.get('kind') == 'definite']
    indirect = [x for x in c['information'].values() if x.get('kind') in ('indirect', 'narrated')]
    if mid == 'O1': return sum(x['room'] == 5 and x['successes'] and x['alone_players'] for x in search) >= 2
    if mid == 'O2':
        first = p.get('first_definite')
        return bool(first and current-first['cycle'] >= 40 and not any(
            x['clue_id'] == first['id'] and x['sender'] == aid for x in g['disclosures']))
    if mid == 'O3': return streak(7, lambda x: all(x['rooms'][other] != 7 for other in x['alive'] if other != str(cid))) >= 6 and any(x['room'] == 7 and x['successes'] for x in search)
    if mid == 'O4': return sum(x >= 65 for x in incoming) >= 4
    if mid == 'O5': return relation >= 85
    if mid == 'O6': return set(targets.get('clue_ids', [])) <= set(c['information']) and len(targets.get('clue_ids', [])) == 3
    if mid == 'O7': return duration(40, lambda x: x['approach'] not in ('interrogate', 'threaten'))
    if mid == 'M01': return len(p['visited']) == 8
    if mid == 'M02': return at(2) >= 10
    if mid == 'M03': return streak(3) >= 6
    if mid == 'M04': return streak(4, lambda x: all(x['rooms'][other] != 4 for other in x['alive'] if other != str(cid))) >= 6
    if mid == 'M05': return sum(x['room'] == 6 and x['successes'] > 0 for x in search) >= 3
    if mid == 'M06': return streak(1, lambda x: not received(x['cycle'])) >= 5
    if mid == 'M07': return sum(x['rooms'][str(cid)] == x['rooms'].get(str(target_cid)) for x in cycles) >= 10
    if mid == 'M08': return len(cycles) >= 20 and all(x['rooms'][str(cid)] != x['rooms'].get(str(target_cid)) for x in cycles[:20])
    if mid == 'M09': return len(cycles) >= 10 and all(x['rooms'][str(cid)] == SHEETS[cid]['start_room'] for x in cycles[:10])
    if mid == 'M10': return sum(sum(r == x['rooms'][str(cid)] for r in x['rooms'].values()) >= 4 for x in cycles) >= 5
    if mid == 'M11': return sum(sum(r == x['rooms'][str(cid)] for r in x['rooms'].values()) == 1 for x in cycles) >= 8
    if mid == 'M12': return sum(x >= 60 for x in incoming) >= 6
    if mid == 'M13': return sum(x >= 70 for x in npc_incoming) >= 3
    if mid == 'M14': return relation >= 70
    if mid == 'M15': return relation >= 80
    if mid == 'M16': return len(cycles) >= 40 and not p.get('relationship_below_30', False)
    if mid == 'M17': return sum(x <= 30 for x in incoming) >= 3
    if mid == 'M18': return max(incoming) >= 80 and min(incoming) <= 20
    if mid == 'M19': return current <= 15 and sum(x >= 60 for x in incoming) >= 3
    if mid == 'M20': return duration(30, lambda x: x['approach'] != 'threaten') and len(agreements) >= 1
    if mid == 'M21': return duration(30, lambda x: x['kind'] != 'explore')
    if mid == 'M22': return duration(30, lambda x: x['kind'] != 'question')
    if mid == 'M23': return duration(30, lambda x: x['kind'] != 'prepare')
    if mid == 'M24': return sum(x['prepared'] and x['kind'] in ('question', 'explore') for x in acts) >= 8
    if mid == 'M25': return len(cycles) >= 20 and all(x['rooms'][str(cid)] != (SHEETS[cid]['start_room'] if i == 0 else cycles[i-1]['rooms'][str(cid)]) for i, x in enumerate(cycles[:20]))
    if mid == 'M26': return len(cycles) >= 40 and not any(x['sender'] == aid and x['kind'] == 'possession' for x in g['disclosures'] if x['cycle'] <= 40)
    if mid == 'M27': return len(cycles) >= 40 and not any(x['proposer'] == aid or (x['recipient'] == aid and x['accepted']) for x in g['trades'].values() if x['cycle'] <= 40)
    if mid == 'M28': return max([sum(x['target'] == y['target'] for x in qs) for y in qs] or [0]) >= 5
    if mid == 'M29': return len({x['target'] for x in success if x['kind'] == 'question'}) >= 7
    if mid == 'M30': return sum(x['approach'] == 'interrogate' for x in success) >= 4
    if mid == 'M31': return sum(x['approach'] == 'threaten' for x in success) >= 3
    if mid == 'M32': return sum(x['target'] is None and x['successes'] > 0 for x in search) >= 6
    if mid == 'M33': return len({x['room'] for x in search if x['successes'] > 0}) >= 3
    if mid == 'M34': return sum(x['approach'] == 'question' and x['successes'] >= 2 for x in qs) >= 3
    if mid == 'M35': return sum(x['approach'] in ('question', 'interrogate') and x['target_mode'] == 'npc' and x['successes'] > 0 for x in qs) >= 6
    if mid == 'M36': return sum(all(x['fulfilled'].values()) for x in trades) >= 3
    if mid == 'M37': return sum(not x['fulfilled'][aid] and any(v for a, v in x['fulfilled'].items() if a != aid) for x in trades) >= 2
    if mid == 'M38': return len({x['recipient'] if x['proposer'] == aid else x['proposer'] for x in agreements}) >= 3
    if mid == 'M39': return sum(x.get('first_sender') not in (None, aid) for x in trades) >= 3
    if mid == 'M40': return all(sum(x['category'] == cat for x in definite) >= 2 for cat in ('suspect', 'weapon', 'place'))
    if mid == 'M41': return len(indirect) >= 4
    if mid == 'M42':
        acquired = [x for x in p['acquired'] if x['room'] == targets.get('place')]
        return any(current-x['cycle'] >= 20 and not any(x['id'] in other['information'] for i, other in g['characters'].items() if int(i) != cid and other['controller']) for x in acquired)
    if mid == 'M43': return len({x['recipient'] for x in g['disclosures'] if x['sender'] == aid and x['kind'] == 'response_definite'}) >= 3
    if mid == 'M44': return sum(x['target'] == target_cid for x in qs) >= 4
    if mid == 'M45': return at(5) >= 10
    if mid == 'M46': return sum(x['target'] == target_cid and x['approach'] in ('interrogate', 'threaten') and x['successes'] > 0 for x in qs) >= 2
    if mid == 'M47': return sum(x['target'] == target_cid and x['approach'] == 'question' and x['successes'] > 0 for x in qs) >= 3
    if mid == 'M48': return len(received()) >= 6
    if mid == 'M49': return len(cycles) >= 15 and not any(x['cycle'] <= 15 for x in received())
    if mid == 'M50': return at(7) >= 8
    if mid == 'M51': return len(definite) >= mission['params']['N']
    raise AssertionError(f'unimplemented mission {mid}')
