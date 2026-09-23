"""Opt-in integration test: local Gerrit on port 18080; creates synthetic data."""
import http.client, importlib.util, json, os, ssl, subprocess, sys, threading, time, uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
from real_gerrit import API
spec=importlib.util.spec_from_file_location('review',ROOT/'skills/gerrit-implement/gerrit_implement.py')
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
work=ROOT/'.local-gerrit'/('implementer-'+uuid.uuid4().hex[:10]);work.mkdir(mode=0o700, parents=True)
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

try:
 check(command('doctor')['tls_verified'], 'HTTPS authentication with custom CA')
 ca=os.environ.pop('GERRIT_CA_FILE')
 fail('Untrusted certificate refused','doctor');os.environ['GERRIT_CA_FILE']=ca
 secret=os.environ['GERRIT_HTTP_PASSWORD'];os.environ['GERRIT_HTTP_PASSWORD']='wrong'
 fail('Wrong password refused','doctor');os.environ['GERRIT_HTTP_PASSWORD']=secret
 task=work/'task';command('start','--project',project,'--branch','main','--task',task)
 repo=task/'repo';source=repo
 check(repo.is_dir(),'Clone absent workspace')
 (repo/'hello.py').write_text('def hello():\n    return "hello"\n')
 message=work/'message.txt';message.write_text('Add greeting function\n\nProvide a reusable greeting.\n\nTest: Python compilation.\n')
 sh('python3','-c','compile(open("hello.py").read(), "hello.py", "exec")',cwd=repo)
 fail('Traversal paths refused','commit','--task',task,'--path','../message.txt','--message-file',message)
 command('commit','--task',task,'--path','hello.py','--message-file',message)
 check(command('push','--task',task)['dry_run'],'Push preview does not upload')
 check(not admin.call('GET','/changes/?q=project:'+r.q(project)),'No change before send')
 first=command('push','--task',task,'--send');check(first['patchset']==1,'Create Gerrit patch set 1')
 check(command('push','--task',task,'--send')['already_uploaded'],'Repeated push reconciles without duplicate')
 stale=work/'stale';command('resume',str(first['number']),'--task',stale)
 (repo/'hello.py').write_text('def hello(name="world"):\n    return "hello " + name\n')
 check('hello(name' in command('diff','--task',task)['unstaged'],'Diff includes unstaged edits')
 command('commit','--task',task,'--path','hello.py','--message-file',message)
 second=command('push','--task',task,'--send')
 check(second['number']==first['number'] and second['patchset']==2 and second['change_id']==first['change_id'],'Amend uploads patch set 2 on same change')
 (stale/'repo'/'hello.py').write_text('old edit\n')
 command('commit','--task',stale,'--path','hello.py','--message-file',message)
 fail('Concurrent patch set refused','push','--task',stale,'--send')
 (repo/'local.txt').write_text('preserve me')
 fail('Dirty tree refuses push','push','--task',task)
 before=git('status','--porcelain')
 resumed=work/'resumed';command('resume',first['link'],'--workspace',repo,'--task',resumed)
 check(git('status','--porcelain')==before and (repo/'local.txt').read_text()=='preserve me','Reuse preserves dirty original workspace')
 source=resumed/'repo'
 (source/'hello.py').write_text('def hello(name="world"):\n    return "Hello, " + name\n')
 command('commit','--task',resumed,'--path','hello.py','--message-file',message)
 third=command('push','--task',resumed,'--send');check(third['patchset']==3 and third['number']==first['number'],'Resume link uploads third patch set')
 check(command('status','--task',resumed)['remote']['current_revision']==third['commit'],'Status confirms server SHA')
 config=(source/'.git'/'config').read_text()
 check(token not in config and token not in (resumed/'task.json').read_text(),'No credentials stored in config or state')
 pending=r.load(resumed/'task.json');pending['pending']='uncertain';r.save(resumed/'task.json',pending)
 check(command('push','--task',resumed)['already_uploaded'],'Pending successful push reconciles by SHA')
 (source/'hello.py').write_text('def hello():\n    return "done"\n')
 command('commit','--task',resumed,'--path','hello.py','--message-file',message)
 pending=r.load(resumed/'task.json');pending['pending']=pending['head'];r.save(resumed/'task.json',pending)
 fail('Unconfirmed push never blindly retries','push','--task',resumed,'--send')
 pending['pending']=None;r.save(resumed/'task.json',pending)
 admin.call('POST','/changes/'+str(first['number'])+'/abandon',{})
 fail('Closed change refused','push','--task',resumed,'--send')
 fail('Push option injection rejected','start','--project',project,'--branch','main%submit','--task',work/'bad')
 message.write_text('Bad title\n\nChange-Id: I'+'1'*40)
 fail('User-supplied Change-Id refused','commit','--task',resumed,'--path','hello.py','--message-file',message)
 print(json.dumps({'passed':len(checks),'checks':checks,'artifacts':str(work)},indent=2))
finally:
 server.shutdown()
