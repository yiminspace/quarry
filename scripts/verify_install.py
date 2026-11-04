#!/usr/bin/env python3
"""Exercise fresh wheel/native installs and an actual Python 1.11 -> Rust upgrade."""
import json, os, pathlib, sqlite3, subprocess, sys, tarfile, tempfile, tomllib, venv
root=pathlib.Path(__file__).resolve().parents[1];dist=pathlib.Path(sys.argv[1]).resolve();wheels=list(dist.glob('*.whl'));assert len(wheels)==1
version=tomllib.loads((root/'Cargo.toml').read_text())['workspace']['package']['version']
def run(args,**kw):return subprocess.check_output([str(x) for x in args],text=True,**kw)
with tempfile.TemporaryDirectory() as tmp:
 t=pathlib.Path(tmp);ws=t/'workspace';ws.mkdir();db=ws/'test.sqlite3'
 with sqlite3.connect(db) as c:c.execute('CREATE TABLE sentinel(value TEXT)');c.execute("INSERT INTO sentinel VALUES('preserved')")
 (t/'config.toml').write_text('');(ws/'connections.toml').write_text('[test]\nurl='+json.dumps('sqlite:///'+str(db))+'\nengine="sqlite"\n')
 env=dict(os.environ,HOME=str(t/'home'),QUARRY_CONFIG=str(t/'config.toml'),QUARRY_UPDATE_CHECK='0');path=t/'fresh';venv.create(path,with_pip=True);python=path/'bin/python'
 def smoke(qy):
  assert run([qy,'--version'],env=env).strip()=='qy '+version
  args=[qy,'--config',t/'config.toml','--workspace',ws,'exec','test','--sql','SELECT value FROM sentinel']
  assert json.loads(run(args,env=dict(env,PATH='/nonexistent')))==[{'value':'preserved'}]
 run([python,'-m','pip','install','--no-deps',wheels[0]],env=env);smoke(path/'bin/qy')
 upgrade=t/'upgrade';venv.create(upgrade,with_pip=True);py=upgrade/'bin/python'
 run([py,'-m','pip','install','quarry-db==1.11.0'],env=env)
 run([py,'-m','pip','install','--force-reinstall','--no-deps',wheels[0]],env=env);smoke(upgrade/'bin/qy')
 run([py,'-c','import importlib.util; assert importlib.util.find_spec("quarry") is None'],env=env)
 archives=list(dist.glob('*.tar.gz'));assert len(archives)==1
 with tarfile.open(archives[0]) as tf:(t/'qy').write_bytes(tf.extractfile('qy').read())
 (t/'qy').chmod(0o755);smoke(t/'qy')
print('Fresh wheel, native archive, Python upgrade and Python-free execution passed')
