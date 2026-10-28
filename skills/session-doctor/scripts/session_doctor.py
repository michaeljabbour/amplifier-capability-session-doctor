#!/usr/bin/env python3
"""Diagnose direct role/content JSONL; write an explicit repair copy, never mutate source."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
from pathlib import Path

MAX_BYTES = 16_000_000


def load(path):
    with Path(path).open('rb') as f: raw=f.read(MAX_BYTES+1)
    if len(raw)>MAX_BYTES: raise ValueError('Transcript exceeds 16 MB inspection budget')
    messages=[]; invalid=[]
    for line,text in enumerate(raw.decode('utf-8').splitlines(),1):
        if not text.strip(): continue
        try: messages.append(json.loads(text))
        except json.JSONDecodeError: invalid.append(line)
    return raw,messages,invalid


def diagnose(messages, invalid=()):
    issues=[]; calls={}; results={}; supported=True
    for i,m in enumerate(messages):
        if not isinstance(m,dict) or m.get('role') not in ('user','assistant','system') or not isinstance(m.get('content'),(str,list)):
            supported=False; issues.append({'kind':'unsupported-envelope','message':i,'severity':'high'}); continue
        if i and isinstance(messages[i-1],dict) and m['role']==messages[i-1].get('role'):
            issues.append({'kind':'consecutive-role-advisory','message':i,'severity':'low'})
        if not isinstance(m['content'],list): continue
        for j,c in enumerate(m['content']):
            if not isinstance(c,dict): supported=False; continue
            if c.get('type')=='tool_use':
                cid=c.get('id')
                if m['role']!='assistant' or not isinstance(cid,str) or not cid or 'input' not in c:
                    supported=False; issues.append({'kind':'malformed-tool-call','message':i,'severity':'high'});continue
                if cid in calls: issues.append({'kind':'duplicate-tool-id','tool_id':cid,'severity':'high'})
                calls[cid]=i
            elif c.get('type')=='tool_result':
                cid=c.get('tool_use_id')
                if m['role']!='user' or not isinstance(cid,str) or not cid:
                    supported=False; issues.append({'kind':'malformed-tool-result','message':i,'severity':'high'});continue
                if cid in results: issues.append({'kind':'duplicate-result','tool_id':cid,'severity':'high'})
                results[cid]=i
    for cid,i in calls.items():
        if cid not in results: issues.append({'kind':'orphaned-tool-call','tool_id':cid,'message':i,'severity':'critical'})
        elif results[cid]!=i+1: issues.append({'kind':'nonadjacent-tool-result','tool_id':cid,'message':i,'severity':'high'})
    for cid,i in results.items():
        if cid not in calls: issues.append({'kind':'result-without-call','tool_id':cid,'message':i,'severity':'high'})
    for line in invalid: issues.append({'kind':'invalid-json-line','line':line,'severity':'high'})
    chars=len(json.dumps(messages))
    if chars>500000: issues.append({'kind':'large-transcript-advisory','characters':chars,'severity':'medium'})
    return {'format':'session-diagnosis/1','supported_envelope':supported and not invalid,'messages':len(messages),
            'issues':issues,'status':'needs-review' if issues else 'no-structural-issues-found',
            'resumability':'unverified; provider acceptance requires a separate live resume'}


def repair_copy(messages, report):
    # Only missing results may be repaired automatically. Never conceal malformed
    # JSON, unknown envelope fields, duplicate IDs, or a delayed real result.
    if not report['supported_envelope'] or any(x['severity']=='high' for x in report['issues']):
        raise ValueError('Ambiguous or unsupported transcript; manual review required')
    orphans={}
    for i in report['issues']:
        if i['kind']=='orphaned-tool-call': orphans.setdefault(i['message'],[]).append(i['tool_id'])
    if not orphans: raise ValueError('No orphaned tool calls to repair')
    out=copy.deepcopy(messages)
    for index,ids in sorted(orphans.items(),reverse=True):
        blocks=[{'type':'tool_result','tool_use_id':cid,'content':'Synthetic repair: tool call was interrupted; no execution result is available.','is_error':True} for cid in ids]
        if index+1<len(out) and out[index+1].get('role')=='user':
            c=out[index+1]['content']; out[index+1]['content']=blocks+([{'type':'text','text':c}] if isinstance(c,str) else c)
        else: out.insert(index+1,{'role':'user','content':blocks})
    return out


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('source',type=Path);p.add_argument('--repair-copy',type=Path)
    a=p.parse_args(argv)
    try:
        raw,messages,invalid=load(a.source);r=diagnose(messages,invalid);r['source_sha256']=hashlib.sha256(raw).hexdigest()
        if a.repair_copy:
            if a.repair_copy.resolve()==a.source.resolve():raise ValueError('Repair destination must differ from source')
            out=repair_copy(messages,r)
            after=diagnose(out)
            if any(x['severity'] in ('high','critical') for x in after['issues']):raise ValueError('Repair still has structural failures; no copy written')
            with a.repair_copy.open('x',encoding='utf-8') as f:
                for m in out: f.write(json.dumps(m,ensure_ascii=False)+'\n')
            r['repair_copy']=str(a.repair_copy);r['verification']=after
        print(json.dumps(r,indent=2));return 0
    except (ValueError,OSError) as e:p.exit(2,f'{type(e).__name__}: {e}\n')

if __name__=='__main__':raise SystemExit(main())
