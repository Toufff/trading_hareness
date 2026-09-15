"""Retain bounded source evidence independently of the three-release rollback window.

An archive is never imported/executed automatically. It contains no runtime env,
database, credentials, interpreter or virtualenv. Dependencies require separate
installation from the retained lock before a human-approved isolated replay.
"""
import hashlib
import io
import json
import os
from pathlib import Path,PurePosixPath
import platform
import re
import zipfile
from .rules import digest,require

MAX_BYTES=2_000_000
MAX_FILES=120
PREFIXES=('app/short_term_lanes/','app/ranking_factors/','app/strategy_governance/')
EXACT={'app/__init__.py','app/short_term_liquidity.py','requirements.txt','requirements.lock','pyproject.toml'}


def _allowed(name):
    path=PurePosixPath(name)
    return not path.is_absolute() and '..' not in path.parts and (name in EXACT or
        any(name.startswith(prefix) and '/' not in name[len(prefix):] and name.endswith('.py') for prefix in PREFIXES))


def archive_sources(*, service_root=None,output_root=None):
    root=Path(service_root) if service_root else Path(__file__).resolve().parents[2]
    files=[p for prefix in PREFIXES for p in (root/prefix).glob('*.py')]
    files += [root/name for name in EXACT if (root/name).is_file()]
    files=sorted(set(files),key=lambda p:p.as_posix())
    require(1<=len(files)<=MAX_FILES,'Source archive file budget exceeded')
    data={}
    for path in files:
        require(root.resolve() in path.resolve().parents,'Source symlink escapes service root')
        name=path.relative_to(root).as_posix();require(_allowed(name),'Source outside archive allowlist')
        raw=path.read_bytes()
        require(not re.search(rb'(?:sk-|sra_v[0-9]_)[A-Za-z0-9_-]{24,}|https?://[^/\s]+:[^/\s]+@',raw),
                'Potential literal credential found; archive refused without revealing it')
        data[name]=raw
    require('requirements.lock' in data and 'app/short_term_liquidity.py' in data,'Dependency lock or market dependency missing')
    require(sum(map(len,data.values()))<=MAX_BYTES,'Source archive byte budget exceeded')
    hashes={name:hashlib.sha256(raw).hexdigest() for name,raw in data.items()}
    fingerprint=digest({name.removeprefix('app/'):sha for name,sha in hashes.items()
        if name.startswith(('app/short_term_lanes/','app/ranking_factors/')) or name=='app/short_term_liquidity.py'})
    runner='app/strategy_governance/experiment_runner.py'
    require(runner in hashes,'Experiment runner source missing')
    manifest={'schema':'strategy-source-archive-v1','files':hashes,'code_hash':fingerprint,
        'runner_hash':hashes[runner],'python_version':platform.python_version(),
        'scope':'source and dependency lock only; no automatic execution or packaged Python environment'}
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        for name,raw in sorted({**data,'manifest.json':json.dumps(manifest,sort_keys=True,ensure_ascii=False).encode()}.items()):
            info=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0));info.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(info,raw)
    raw=buffer.getvalue();require(len(raw)<=MAX_BYTES,'Compressed source budget exceeded')
    sha=hashlib.sha256(raw).hexdigest()
    target=Path(output_root) if output_root else Path(os.getenv('QUANT_DATA_DIR','G:/StockPlatform/data/research'))/'governance/source-archives'
    target.mkdir(parents=True,exist_ok=True);path=target/(sha+'.zip')
    if not path.exists():
        with path.open('xb') as file:file.write(raw)
    require(hashlib.sha256(path.read_bytes()).hexdigest()==sha,'Existing source archive content mismatch')
    return {'path':str(path.resolve()),'sha256':sha,'code_hash':fingerprint,'runner_hash':hashes[runner],
        'file_count':len(data),'uncompressed_bytes':sum(map(len,data.values())),'schema':manifest['schema']}


def verify_source_archive(reference,code_hash,runner_hash):
    require(isinstance(reference,dict),'missing_source_archive: legacy experiment is not source-reproducible; create a new frozen revision')
    path=Path(reference.get('path',''))
    require(path.is_file() and path.stat().st_size<=MAX_BYTES,'Source archive missing or oversized')
    raw=path.read_bytes();require(hashlib.sha256(raw).hexdigest()==reference.get('sha256'),'Source archive hash mismatch')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos=archive.infolist();names=[i.filename for i in infos]
        require(len(names)==len(set(names)) and len(names)<=MAX_FILES+1,'Duplicate/oversized source manifest')
        require(sum(i.file_size for i in infos)<=MAX_BYTES+50_000,'Source archive expansion budget exceeded')
        require(all(name=='manifest.json' or _allowed(name) for name in names),'Unsafe source archive member')
        manifest=json.loads(archive.read('manifest.json'))
        require(manifest.get('schema')=='strategy-source-archive-v1','Unknown source archive schema')
        require(set(manifest['files'])==set(names)-{'manifest.json'},'Source inventory mismatch')
        for name,sha in manifest['files'].items():
            require(hashlib.sha256(archive.read(name)).hexdigest()==sha,'Archived source content changed')
        rebuilt=digest({name.removeprefix('app/'):sha for name,sha in manifest['files'].items()
            if name.startswith(('app/short_term_lanes/','app/ranking_factors/')) or name=='app/short_term_liquidity.py'})
        require(rebuilt==code_hash,'Archived strategy inventory does not reproduce code fingerprint')
        require(manifest['files'].get('app/strategy_governance/experiment_runner.py')==runner_hash and 'requirements.lock' in manifest['files'],
                'Archived runner or dependency lock missing/mismatched')
        require(manifest['code_hash']==code_hash and manifest['runner_hash']==runner_hash,'Archive does not match frozen source identity')
    return {'status':'verified','file_count':len(names)-1,'archive_hash':reference['sha256']}
