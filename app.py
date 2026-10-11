#!/usr/bin/env python3
"""Local chat GM. Python 3.10+, no third-party packages required."""
import argparse
import json
import sqlite3
import threading
import webbrowser
from contextlib import closing
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from engine import new_game, public, add, apply, local_intent, SUSPECTS, WEAPONS, PLACES, JOBS
from llm import Zen
import multi

ROOT=Path(__file__).resolve().parent

class Store:
    def __init__(self,path):
        self.path=path;self.lock=threading.Lock();self.game_locks={}
        with closing(sqlite3.connect(path)) as db:
            with db:db.execute('CREATE TABLE IF NOT EXISTS games(id TEXT PRIMARY KEY, state TEXT NOT NULL)')
    def get(self,id):
        with closing(sqlite3.connect(self.path)) as db:r=db.execute('SELECT state FROM games WHERE id=?',(id,)).fetchone()
        if not r:raise ValueError('사건을 찾지 못했습니다. 새 사건을 시작하세요.')
        return json.loads(r[0])
    def save(self,g):
        with closing(sqlite3.connect(self.path)) as db:
            with db:db.execute('INSERT OR REPLACE INTO games VALUES (?,?)',(g['id'],json.dumps(g,ensure_ascii=False)))
    def for_game(self,id):
        with self.lock:return self.game_locks.setdefault(id,threading.Lock())

class Handler(BaseHTTPRequestHandler):
    def send(self,status,value,ctype='application/json'):
        data=json.dumps(value,ensure_ascii=False).encode() if ctype=='application/json' else value
        self.send_response(status);self.send_header('Content-Type',ctype+'; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def do_GET(self):
        if self.path=='/':self.send(200,(ROOT/'index.html').read_bytes(),'text/html')
        elif self.path=='/multi':self.send(200,(ROOT/'multi.html').read_bytes(),'text/html')
        elif self.path=='/catalog':self.send(200,{'suspects':SUSPECTS,'weapons':WEAPONS,'places':PLACES,'jobs':JOBS})
        else:self.send(404,{'error':'찾을 수 없습니다.'})
    def do_POST(self):
        try:
            origin=self.headers.get('Origin')
            if origin and urlparse(origin).netloc!=self.headers.get('Host'):raise ValueError('로컬 화면에서 실행해주세요.')
            if self.headers.get('X-GM-Client')!='local':raise ValueError('올바른 클라이언트 요청이 아닙니다.')
            size=int(self.headers.get('Content-Length',0))
            if size<2 or size>18000:raise ValueError('입력 크기를 확인하세요.')
            d=json.loads(self.rfile.read(size))
            if not isinstance(d,dict):raise ValueError('잘못된 입력입니다.')
            if self.path.startswith('/multi/'):
                self.handle_multi(d);return
            if self.path=='/start':
                g=new_game(str(d.get('name','여행자')),int(d.get('role',0)),int(d.get('stat',4)),d.get('goal','사건 해결'))
                self.server.store.save(g);self.send(200,public(g));return
            id=d.get('id')
            if not isinstance(id,str) or len(id)>100:raise ValueError('먼저 사건을 시작하세요.')
            with self.server.store.for_game(id):
                g=self.server.store.get(id)
                if g.get('kind')=='multi':raise ValueError('멀티에이전트 화면에서 실행해주세요.')
                if self.path=='/state':self.send(200,public(g));return
                if self.path=='/export':
                    p=public(g);self.send(200,{'title':'오네이트 익스프레스 수사 기록','character':p['name'],'messages':p['messages'],'clues':p['clues'],'ended':p['ended']});return
                if self.path not in ('/chat','/roll','/approach','/accuse'):raise ValueError('알 수 없는 명령입니다.')
                if g['ended']:raise ValueError('사건이 종료되었습니다. 새 여행을 시작하세요.')
                key=d.get('key','');model=d.get('model','gpt-6-luna')
                if not isinstance(key,str) or not isinstance(model,str) or len(key)>500 or len(model)>100:raise ValueError('API 설정을 확인하세요.')
                zen=Zen(key,model,d.get('protocol','responses'),d.get('connection','openai'),g['id']);notice=None
                if self.path=='/approach':
                    if not g['pending']:raise ValueError('기다리는 판정이 없습니다.')
                    approach=d.get('approach')
                    if approach not in ('compassion','brutality'):raise ValueError('방식을 확인하세요.')
                    g['pending']['approach']=approach
                    mode='강경함' if approach=='brutality' else '공감'
                    add(g,'system',mode+' 방식으로 판정합니다. 같은 숫자는 실패입니다.')
                else:
                    if self.path=='/roll':text='주사위를 굴린다';intent={'kind':'roll'}
                    elif self.path=='/accuse':
                        text='최종 지목';intent={'kind':'accuse'}
                        for k,limit in [('suspect',12),('weapon',8),('place',8)]:
                            v=d.get(k)
                            if not isinstance(v,int) or isinstance(v,bool) or not 0<=v<limit:raise ValueError('지목 항목을 확인하세요.')
                            intent[k]=v
                        from engine import display
                        text='최종 지목: '+', '.join(display(g,k,intent[k]) for k in ('suspect','weapon','place'))
                    else:
                        text=str(d.get('text','')).strip()
                        if not text or len(text)>1200:raise ValueError('행동을 1~1200자로 적어주세요.')
                        # Pending rolls are handled locally; an LLM cannot bypass them.
                        if g['pending']:intent=local_intent(g,text)
                        else:intent,notice=zen.intent(g,text)
                    add(g,'player',text)
                    result=apply(g,intent,text)
                    if intent['kind']=='accuse' or g['ended']:
                        add(g,'gm',result)
                    else:
                        narrative,error=zen.narrate(g,text,result)
                        notice=notice or error
                        if key and not error:
                            add(g,'gm',narrative);add(g,'system',result)
                        else:add(g,'gm',result)
                    if g['target'] is not None and intent['kind'] in ('talk','check','find','roll'):
                        memory=g['npc_memory'][str(g['target'])]
                        memory.append({'who':'gm','text':result[:1500]})
                        del memory[:-20]
                self.server.store.save(g)
                p=public(g);p['connection_notice']=notice;p['mode']='llm' if key and not notice else 'offline'
                self.send(200,p)
        except (ValueError,TypeError,KeyError,json.JSONDecodeError) as e:self.send(400,{'error':str(e)[:250]})
        except Exception:self.send(500,{'error':'서버 오류가 발생했습니다. 입력을 확인하고 다시 시도해주세요.'})
    def log_message(self,*args):pass

    def handle_multi(self,d):
        if self.path=='/multi/start':
            g=multi.create(d.get('n',3),d.get('seed',0))
            self.server.store.save(g)
            self.send(200,{'id':g['id'],'gm_token':g['gm_token'],
                           'agent_credentials':{aid:a['token'] for aid,a in g['agents'].items()},
                           'state':multi.view(g)});return
        game_id=d.get('id')
        if not isinstance(game_id,str) or len(game_id)>100:raise ValueError('사건 ID를 확인하세요.')
        with self.server.store.for_game(game_id):
            g=self.server.store.get(game_id)
            if g.get('kind')!='multi':raise ValueError('멀티에이전트 사건이 아닙니다.')
            aid=d.get('agent_id')
            if self.path=='/multi/action':
                if not isinstance(aid,str):raise ValueError('agent_id가 필요합니다.')
                multi.authorize(g,d.get('token'),aid)
                request=d.get('request',{})
                if not isinstance(request,dict):raise ValueError('행동 요청 형식을 확인하세요.')
                result=multi.execute(g,request | {'agent_id':aid})
                self.server.store.save(g);self.send(200,result);return
            if self.path=='/multi/observe' and aid:
                multi.authorize(g,d.get('token'),aid)
                self.send(200,multi.view(g,aid));return
            multi.authorize(g,d.get('token'))
            if self.path in ('/multi/observe','/multi/export'):
                self.send(200,multi.view(g));return
            if self.path=='/multi/fail':
                multi.fail(g,aid);self.server.store.save(g)
            elif self.path=='/multi/pause':
                g['status']='paused';self.server.store.save(g)
            elif self.path=='/multi/run':
                key=d.get('key','');model=d.get('model','gpt-6-luna')
                if not isinstance(key,str) or len(key)>500 or not isinstance(model,str) or len(model)>100:
                    raise ValueError('API 설정을 확인하세요.')
                gateway=Zen(key,model,d.get('protocol','responses'),d.get('connection','openai'),g['id'])
                if not g['ended']:
                    g['status']='ready';self.server.store.save(g)
                    self.server.runtime.start(game_id,gateway)
            else:raise ValueError('알 수 없는 멀티에이전트 명령입니다.')
            self.send(200,multi.view(g))

def main():
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,default=8765);p.add_argument('--no-browser',action='store_true');p.add_argument('--data',default=str(ROOT/'data'/'games.sqlite3'));args=p.parse_args()
    Path(args.data).parent.mkdir(parents=True,exist_ok=True)
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler);server.store=Store(args.data)
    server.runtime=multi.Runtime(server.store)
    url=f'http://127.0.0.1:{args.port}/';print('오네이트 익스프레스 GM:',url,'(종료: Ctrl+C)',flush=True)
    if not args.no_browser:threading.Timer(.5,lambda:webbrowser.open(url)).start()
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        with server.runtime.jobs_lock:
            ids=list(server.runtime.running)
        for game_id in ids:
            with server.store.for_game(game_id):
                g=server.store.get(game_id)
                if not g['ended']:g['status']='paused';server.store.save(g)
        server.runtime.close();server.server_close()
if __name__=='__main__':main()
