"""Opt-in local Gerrit integration test; creates synthetic accounts and changes."""
import base64, copy, hashlib, http.client, importlib.util, json, os, ssl, subprocess, sys, threading, time, uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
from real_gerrit import API
spec=importlib.util.spec_from_file_location('review',ROOT/'skills/gerrit-review/gerrit_review.py')
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
work=ROOT/'.local-gerrit'/('reviewer-'+uuid.uuid4().hex[:10]);work.mkdir(mode=0o700, parents=True)
# Isolate the test from any installed personal config; synthetic env values below win.
(work/'config.json').write_text('{}')
os.environ['GERRIT_CONFIG']=str(work/'config.json')
checks=[]
def check(ok,label):
 assert ok,label
 checks.append(label); print('PASS',label,flush=True)
def sh(*cmd,cwd=None,env=None):
 p=subprocess.run(cmd,cwd=cwd,env=env,capture_output=True,text=True)
 if p.returncode: raise AssertionError(str(cmd[:2])+': '+p.stderr[-1500:])
 return p.stdout.strip()
# Self-signed certificate trusted explicitly; no public CA or network required.
sh('openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','2','-keyout',str(work/'server.key'),'-out',str(work/'ca.pem'),'-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1')
class Proxy(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def forward(self):
  length=int(self.headers.get('Content-Length','0'));body=self.rfile.read(length) if length else None
  conn=http.client.HTTPConnection('127.0.0.1',18080,timeout=60)
  headers={k:v for k,v in self.headers.items() if k.lower() not in ('host','connection','accept-encoding')}
  conn.request(self.command,self.path,body,headers);resp=conn.getresponse();data=resp.read()
  self.send_response(resp.status)
  for k,v in resp.getheaders():
   if k.lower() not in ('connection','transfer-encoding','content-length'):self.send_header(k,v)
  self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);conn.close()
 do_GET=forward;do_POST=forward
server=ThreadingHTTPServer(('127.0.0.1',0),Proxy)
ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(work/'ca.pem',work/'server.key');server.socket=ctx.wrap_socket(server.socket,server_side=True)
threading.Thread(target=server.serve_forever,daemon=True).start()
https=f'https://127.0.0.1:{server.server_port}'
admin=API('http://127.0.0.1:18080','admin','secret')
project='reviewer-test/'+uuid.uuid4().hex[:10]
admin.call('PUT','/projects/'+r.q(project),{'create_empty_commit':True,'branches':['main']})
admin.call('POST','/projects/'+r.q(project)+'/access',{'add':{'refs/heads/*':{'permissions':{'push':{'rules':{'global:Registered-Users':{'action':'ALLOW'}}},'label-Code-Review':{'rules':{'global:Registered-Users':{'action':'ALLOW','min':-2,'max':2}}}}}}})
user='reviewer-'+uuid.uuid4().hex[:10]
account=admin.call('PUT','/accounts/'+user,{'name':'Review Bot Test','email':user+'@example.com'})
token=admin.call('PUT',f"/accounts/{account['_account_id']}/tokens/test",{'lifetime':'1d'})['token']
os.environ.update(GERRIT_URL=https,GERRIT_USER=user,GERRIT_HTTP_PASSWORD=token,GERRIT_CA_FILE=str(work/'ca.pem'))
client=r.Client(r.parser().parse_args(['doctor']))
def command(*args):return r.main(list(map(str,args)))
def fail(label,*args):
 try:command(*args)
 except r.ReviewError:check(True,label);return
 raise AssertionError('Did not reject: '+label)
def git(*args):
 result=client.git(*args,cwd=source,check=False)
 if result.returncode: raise AssertionError(result.stderr.decode().replace(token,'[redacted]'))
 return result.stdout.decode().strip()
source=work/'source';client.git('clone',https+'/a/'+project,str(source))
git('config','user.name','Review Bot Test');git('config','user.email',user+'@example.com')
(source/'base.py').write_text('def sum_items(items):\n    return sum(items)\n')
(source/'remove.txt').write_text('remove me\n')
(source/'old.txt').write_text('stable line\nsecond line\n')
(source/'AGENTS.md').write_text('Use project conventions.\n')
git('add','.');git('commit','-m','Seed base');git('push','origin','HEAD:main')
base=git('rev-parse','HEAD')
(source/'base.py').write_text('def sum_items(items):\n    return sum(items) + 1\n# 😀 unicode\n')
git('mv','old.txt','new.txt');git('rm','remove.txt')
(source/'new.py').write_text('print("new")\n')
(source/'binary.bin').write_bytes(b'\x00\x01binary')
changeid='I'+hashlib.sha1(project.encode()).hexdigest()
git('add','.');git('commit','-m','Review helper integration\n\nChange-Id: '+changeid);git('push','origin','HEAD:refs/for/main')
change=admin.call('GET','/changes/?q=change:'+changeid)[0];num=str(change['_number']);first=git('rev-parse','HEAD')
# Preserve a dirty main workspace during prepare.
(source/'base.py').write_text('dirty local edit\n');(source/'untracked.txt').write_text('keep me')
status_before=git('status','--porcelain');head_before=git('rev-parse','HEAD')
try:
 check(command('doctor')['tls_verified'],'HTTPS doctor trusts supplied CA and authenticates reviewer')
 fail('Missing CA rejects the private certificate','--ca-file',str(work/'missing.pem'),'doctor') if False else None
 old=os.environ.pop('GERRIT_CA_FILE')
 fail('Untrusted HTTPS certificate rejected','doctor');os.environ['GERRIT_CA_FILE']=old
 bad=os.environ['GERRIT_HTTP_PASSWORD'];os.environ['GERRIT_HTTP_PASSWORD']='wrong'
 fail('Wrong HTTP credential rejected','doctor');os.environ['GERRIT_HTTP_PASSWORD']=bad
 check(command('get',num)['summary']['number']==int(num),'Numeric change resolution')
 check(command('get',changeid)['summary']['number']==int(num),'Change-Id resolution')
 check(command('get',https+'/c/'+project+'/+/'+num+'/1')['summary']['patchset']==1,'Full Gerrit link pins patch set')
 check(len(command('list','--query','project:'+project,'--limit','1')['changes'])==1,'Query discovery')
 bundle=work/'bundle';prepared=command('prepare',num,'--output',bundle)
 check(git('status','--porcelain')==status_before and git('rev-parse','HEAD')==head_before,'Independent remote clone preserves unrelated dirty checkout')
 check((source/'untracked.txt').read_text()=='keep me','Untracked source file preserved')
 state=r.bundle_at(bundle)
 check(state['baseline']==base and state['base']==base and state['revision']==first,'Baseline/base/head use correct commits')
 check((bundle/'baseline'/'base.py').read_text()=='def sum_items(items):\n    return sum(items)\n','Full baseline file is available')
 check(command('show','--bundle',bundle,'--path','base.py')['lines'][2]['utf16_length']==12,'UTF-16 line lengths returned')
 check(command('show','--bundle',bundle,'--path','/COMMIT_MSG')['total_lines']>0,'Gerrit commit-message content available')
 clonebundle=work/'clone-bundle';command('prepare',num,'--output',clonebundle)
 check((clonebundle/'head'/'base.py').exists(),'Absent workspace clones over authenticated HTTPS using same CA')
 plan=bundle/'plan.json';command('plan','--bundle',bundle,'--out',plan,'--notify','NONE')
 command('comment','--plan',plan,'--path','base.py','--range','2:4-2:10','--text','Range comment')
 command('comment','--plan',plan,'--path','base.py','--range','3:2-3:4','--text','Unicode range')
 command('comment','--plan',plan,'--path','new.txt','--side','PARENT','--line','1','--text','Rename old side')
 command('comment','--plan',plan,'--path','remove.txt','--side','PARENT','--line','1','--text','Deleted old side')
 command('comment','--plan',plan,'--path','binary.bin','--text','Binary file note','--resolved')
 command('vote','--plan',plan,'--score=-1');command('message','--plan',plan,'--text','Integration review summary')
 valid=command('validate','--plan',plan);check(valid['valid'],'Range, Unicode, rename, deletion, binary file and vote validate')
 before=admin.call('GET',f'/changes/{num}/comments')
 check(command('publish','--plan',plan)['dry_run'],'Publish without --send is a dry run')
 check(admin.call('GET',f'/changes/{num}/comments')==before,'Dry run did not create comments')
 # A Gerrit draft should not be published by the review helper.
 account_api=API('http://127.0.0.1:18080',user,token)
 draft=account_api.call('PUT',f'/changes/{num}/revisions/{first}/drafts',{'path':'base.py','line':1,'message':'UNRELATED_DRAFT'})
 sent=command('publish','--plan',plan,'--send');check(sent['posted'] and sent['status']['observed_on_server'],'Real review POST and receipt verification')
 posted=admin.call('GET',f'/changes/{num}/comments')
 flat=[c for entries in posted.values() for c in entries]
 check(len(flat)==5,'All five staged comments published once')
 range_comment=next(c for c in flat if c['message']=='Range comment')
 check(range_comment['range']=={'start_line':2,'start_character':4,'end_line':2,'end_character':10},'Exact ranged coordinates round-trip through Gerrit')
 check(any(c['message']=='UNRELATED_DRAFT' for entries in account_api.call('GET',f'/changes/{num}/drafts').values() for c in entries),'Unrelated Gerrit draft stays private')
 check(command('publish','--plan',plan,'--send')['already_posted'],'Repeated publish is detected, no duplicated POST')
 check(len([c for es in admin.call('GET',f'/changes/{num}/comments').values() for c in es])==5,'Duplicate prevention verified against live server')
 labels=command('status','--plan',plan)['labels_now']['Code-Review']['all']
 check(any(v['_account_id']==account['_account_id'] and v['value']==-1 for v in labels),'Actual -1 score recorded')
 rp=bundle/'reply.json';command('plan','--bundle',bundle,'--out',rp,'--notify','NONE');command('reply','--plan',rp,'--comment-id',range_comment['id'],'--text','Verified the behavior','--resolved');command('vote','--plan',rp,'--score=1');command('publish','--plan',rp,'--send')
 check(any(c.get('in_reply_to')==range_comment['id'] and not c['unresolved'] for es in admin.call('GET',f'/changes/{num}/comments').values() for c in es),'Reply ID, inherited range, resolution and +1 submission work')
 invalid=bundle/'invalid.json';command('plan','--bundle',bundle,'--out',invalid);command('comment','--plan',invalid,'--path','base.py','--line','999','--text','Wrong position');fail('Out-of-bounds comment refused before POST','validate','--plan',invalid)
 badvote=bundle/'vote.json';command('plan','--bundle',bundle,'--out',badvote);command('vote','--plan',badvote,'--score=99');fail('Unavailable label score refused','validate','--plan',badvote)
 # A new patch set invalidates the old reviewed bundle.
 git('checkout','--','base.py');(source/'base.py').write_text('def sum_items(items):\n    return sum(items) + 2\n# 😀 unicode\n')
 git('add','base.py');git('commit','--amend','--no-edit');git('push','origin','HEAD:refs/for/main')
 fail('New patch set blocks old plan before POST','publish','--plan',badvote,'--send')
 secondbundle=work/'second';command('prepare',num,'--output',secondbundle)
 oldreply=secondbundle/'reply-old.json';command('plan','--bundle',secondbundle,'--out',oldreply,'--notify','NONE');command('reply','--plan',oldreply,'--comment-id',range_comment['id'],'--text','On current patch this still applies')
 fail('Old-patch reply requires explicit verified coordinates','validate','--plan',oldreply)
 content=r.load(oldreply);content['comments']=[];r.save(oldreply,content)
 command('reply','--plan',oldreply,'--comment-id',range_comment['id'],'--path','base.py','--line','2','--text','Explicit current anchor');command('publish','--plan',oldreply,'--send')
 check(any(c.get('in_reply_to')==range_comment['id'] and c['patch_set']==2 for es in admin.call('GET',f'/changes/{num}/comments').values() for c in es),'Cross-patch-set reply with verified anchor published')
 # Simulate an ambiguous transport result by prewriting the pending receipt.
 uncertain=secondbundle/'uncertain.json';command('plan','--bundle',secondbundle,'--out',uncertain);command('message','--plan',uncertain,'--text','Do not blindly retry')
 up=r.load(uncertain);r.save(r.receipt_path(uncertain),{'state':'pending','plan_hash':hashlib.sha256(r.encoded(up)).hexdigest()})
 fail('Unconfirmed prior write is not automatically retried','publish','--plan',uncertain,'--send')

 # Neutral and strong scores remain constrained by the account's live labels.
 for score in [0,2,-2]:
  vp=secondbundle/('score-'+str(score)+'.json');command('plan','--bundle',secondbundle,'--out',vp,'--notify','NONE');command('vote','--plan',vp,'--score='+str(score));result=command('publish','--plan',vp,'--send')
  check(result['status']['account_votes_now'].get('Code-Review')==score,'Actual permitted score '+str(score)+' round-trips')
 # Branch advance followed by rebase distinguishes original baseline from current parent.
 git('checkout','--detach',base);(source/'upstream.txt').write_text('upstream context\n');git('add','upstream.txt');git('commit','-m','Upstream change');upstream=git('rev-parse','HEAD');git('push','origin','HEAD:main')
 current=admin.call('GET',f'/changes/{num}/detail?o=CURRENT_REVISION')['current_revision'];git('checkout','--detach',current);git('rebase',upstream);git('push','origin','HEAD:refs/for/main')
 rebased=work/'rebased';command('prepare',num,'--output',rebased);rb=r.bundle_at(rebased)
 check(rb['baseline']==base and rb['base']==upstream,'Rebase keeps original baseline separate from current-parent review base')
 check('upstream.txt' not in r.load(rebased/'files.json'),'Upstream-only file is excluded from review diff')
 # Closed changes can be inspected but never voted on by this workflow.
 cp=rebased/'closed.json';command('plan','--bundle',rebased,'--out',cp);command('message','--plan',cp,'--text','Should not post on closed change')
 admin.call('POST',f'/changes/{num}/abandon',{'message':'Synthetic closed-state test'})
 fail('Closed change refuses review publication','publish','--plan',cp,'--send')
 # Build an actual merge review; no implicit parent interpretation.
 git('checkout','--detach',upstream);(source/'left.txt').write_text('left\n');git('add','left.txt');git('commit','-m','Left branch');left=git('rev-parse','HEAD')
 git('push','origin','HEAD:main')
 git('checkout','--detach',upstream);(source/'right.txt').write_text('right\n');git('add','right.txt');git('commit','-m','Right branch\n\nChange-Id: I'+hashlib.sha1((project+'right').encode()).hexdigest());right=git('rev-parse','HEAD')
 git('checkout','--detach',left)
 mergeid='I'+hashlib.sha1((project+'merge').encode()).hexdigest()
 git('merge','--no-ff',right,'-m','Merge review\n\nChange-Id: '+mergeid)
 # Allow this test-only project's merge review to include its side branch.
 admin.call('PUT','/projects/'+r.q(project)+'/config',{'reject_implicit_merges':'FALSE'})
 git('push','origin','HEAD:refs/for/main')
 mergechange=admin.call('GET','/changes/?q=change:'+mergeid)[0];mn=str(mergechange['_number'])
 fail('Merge prepare refuses an implicit parent','prepare',mn,'--output',work/'merge-no-parent')
 mb=work/'merge';command('prepare',mn,'--parent','1','--output',mb)
 check(r.bundle_at(mb)['merge'] and r.bundle_at(mb)['base']==left,'Explicit merge parent selects correct base')
 mp=mb/'plan.json';command('plan','--bundle',mb,'--out',mp,'--notify','NONE');command('comment','--plan',mp,'--path','/COMMIT_MSG','--text','Merge reviewed against parent 1');command('publish','--plan',mp,'--send')
 check(command('status','--plan',mp)['observed_on_server'],'Merge file-level review posts on real server')
 # Root commit in a separate empty project.
 rootproject=project+'-root';admin.call('PUT','/projects/'+r.q(rootproject),{'branches':['main']})
 rootwork=work/'root-source';client.git('clone',https+'/a/'+rootproject,str(rootwork))
 client.git('config','user.name','Review Bot Test',cwd=rootwork);client.git('config','user.email',user+'@example.com',cwd=rootwork)
 (rootwork/'root.txt').write_text('root content\n');client.git('add','.',cwd=rootwork)
 rid='I'+hashlib.sha1(rootproject.encode()).hexdigest();client.git('commit','-m','Root review\n\nChange-Id: '+rid,cwd=rootwork);client.git('push','origin','HEAD:refs/for/main',cwd=rootwork)
 rn=str(admin.call('GET','/changes/?q=change:'+rid)[0]['_number']);rootbundle=work/'root-bundle';command('prepare',rn,'--output',rootbundle)
 check(r.bundle_at(rootbundle)['root_commit'] and not (rootbundle/'baseline'/'root.txt').exists(),'Root commit creates an empty synthetic baseline')

 print('SUCCESS',len(checks),'checks',work,flush=True)
finally:
 r.save(work/'results.json',{'checks':checks,'count':len(checks),'project':project,'change':num,'server':'Gerrit 3.13.4 via local TLS reverse proxy','date':r.now()})
 server.shutdown();server.server_close()
