#!/usr/bin/env python3
"""Package the very same executable shipped in the wheel as a native archive."""
import argparse, hashlib, pathlib, tarfile, tempfile, zipfile, tomllib
p=argparse.ArgumentParser();p.add_argument('--asset',required=True);p.add_argument('--output',type=pathlib.Path,required=True);a=p.parse_args()
root=pathlib.Path(__file__).resolve().parents[1];version=tomllib.loads((root/'Cargo.toml').read_text())['workspace']['package']['version'];wheels=list(a.output.glob('*.whl'));assert len(wheels)==1
with tempfile.TemporaryDirectory() as d:
 d=pathlib.Path(d)
 with zipfile.ZipFile(wheels[0]) as z:
  matches=[n for n in z.namelist() if n.endswith('.data/scripts/qy')];assert len(matches)==1
  assert not any('.libs/' in n for n in z.namelist()), 'native archive requires bundled library packaging'
  binary=d/'qy';binary.write_bytes(z.read(matches[0]));binary.chmod(0o755)
 archive=a.output/f'quarry-{version}-{a.asset}.tar.gz'
 with tarfile.open(archive,'w:gz') as tf:
  tf.add(binary,arcname='qy');tf.add(root/'LICENSE',arcname='LICENSE');tf.add(root/'docs/install.md',arcname='INSTALL.md')
 pathlib.Path(str(archive)+'.sha256').write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name+'\n')
print(archive)
