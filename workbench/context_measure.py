"""Read-only text volume estimates for actual default workflow entrypoints."""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
from pathlib import Path

RULES = ['AGENTS.md', *[f'.agents/skills/{name}/SKILL.md' for name in ['workspace-item-design','workspace-writing-plan','workspace-execute-plan','workspace-verify']], '.agents/skills/workspace-verify/references/evidence.md']
BASELINE = 13561


def estimate_tokens(text: str) -> dict:
    cjk = sum(any(a <= ord(c) <= b for a, b in [(0x3000,0x303F),(0x3040,0x30FF),(0x3400,0x4DBF),(0x4E00,0x9FFF),(0xFF00,0xFFEF)]) for c in text)
    other = sum(len(c.encode()) for c in text if not any(a <= ord(c) <= b for a,b in [(0x3000,0x303F),(0x3040,0x30FF),(0x3400,0x4DBF),(0x4E00,0x9FFF),(0xFF00,0xFFEF)]))
    return {'bytes': len(text.encode()), 'chars': len(text), 'cjkChars': cjk, 'estTokens': cjk + (other + 3)//4}


def build_report(root: Path, item=None, task=None):
    root = root.resolve()
    rules = [{'path': name, **estimate_tokens((root/name).read_text())} for name in RULES]
    queries = [['status','--json']] if item is None else [['brief',item,'--json'], *([['brief',item,'--task',task,'--json']] if task else [])]
    commands = []
    for arguments in queries:
        result = subprocess.run([sys.executable,'-B',str(root/'scripts/kit.py'),*arguments],cwd=root,capture_output=True,text=True,timeout=30)
        commands.append({'arguments': arguments, 'exitCode': result.returncode, **estimate_tokens(result.stdout)})
    total = sum(row['estTokens'] for row in rules)
    return {'schemaVersion':2,'tokenModel':'CJK 1 char/token; other UTF-8 bytes/4 estimate, not billed usage',
            'rules':rules,'totalRuleEstTokens':total,'baselineRuleEstTokens':BASELINE,
            'targetMet':total <= 9000 and rules[0]['estTokens'] <= 1200,'commands':commands}


def build_parser():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path.cwd());parser.add_argument('--item');parser.add_argument('--task');parser.add_argument('--json',action='store_true')
    return parser


def main(argv=None):
    args=build_parser().parse_args(argv)
    print(json.dumps(build_report(args.root,args.item,args.task),ensure_ascii=False,indent=None if args.json else 2));return 0

if __name__=='__main__':raise SystemExit(main())
