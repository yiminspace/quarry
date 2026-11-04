#!/usr/bin/env python3
"""Packaged GUI protocol acceptance; only disposable SQLite data and localhost."""
import json,pathlib,re,signal,socket,subprocess,sys,tempfile,time,urllib.request,urllib.error
qy=pathlib.Path(sys.argv[1]).resolve()
with tempfile.TemporaryDirectory() as tmp:
 p=pathlib.Path(tmp);(p/'config.toml').write_text('');(p/'connections.toml').write_text('[smoke]\nengine="sqlite"\nurl="sqlite:///probe.sqlite3"\n')
 import sqlite3
 with sqlite3.connect(p/'probe.sqlite3') as c:c.execute('CREATE TABLE marker(value TEXT)');c.execute("INSERT INTO marker VALUES('preserved')")
 with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
 log=open(p/'gui.log','wb');proc=subprocess.Popen([str(qy),'--config',str(p/'config.toml'),'--workspace',str(p),'gui','--no-browser','--port',str(port)],stdout=log,stderr=log)
 opener=urllib.request.build_opener(urllib.request.ProxyHandler({}));base=f'http://127.0.0.1:{port}'
 try:
  for _ in range(100):
   assert proc.poll() is None
   try:
    with opener.open(base+'/api/version',timeout=1) as r:version=json.load(r)
    break
   except OSError:time.sleep(.1)
  else:raise AssertionError('GUI never became ready')
  with opener.open(base+'/quarry/',timeout=5) as r:html=r.read().decode()
  js=re.search(r'src="([^"]+\.js)"',html).group(1)
  with opener.open(base+js,timeout=5) as r:assert len(r.read())>1000
  req=urllib.request.Request(base+'/api/query',data=json.dumps({'db':'smoke','sql':'SELECT value FROM marker'}).encode(),headers={'Content-Type':'application/json'},method='POST')
  with opener.open(req,timeout=5) as r:assert json.load(r)['rows']==[{'value':'preserved'}]
  try:opener.open(urllib.request.Request(base+'/api/version',headers={'Host':'evil.invalid'}),timeout=5);raise AssertionError('foreignHost accepted')
  except urllib.error.HTTPError as e:assert e.code==403
 finally:
  proc.send_signal(signal.SIGINT);proc.wait(timeout=10);log.close()
print('Packaged GUI assets, SQLite API and origin boundary passed:',version)
