"""Read-only text volume estimates for actual default workflow entrypoints."""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
from pathlib import Path
from workbench.resources import KIT_ROOT

RULES = ['AGENTS.md', *[f'.agents/skills/{name}/SKILL.md' for name in ['workspace-item-design','workspace-writing-plan','workspace-execute-plan','workspace-verify']], '.agents/skills/workspace-verify/references/evidence.md']
BASELINE = 13561


def estimate_tokens(text: str) -> dict:
    cjk = sum(any(a <= ord(c) <= b for a, b in [(0x3000,0x303F),(0x3040,0x30FF),(0x3400,0x4DBF),(0x4E00,0x9FFF),(0xFF00,0xFFEF)]) for c in text)
    other = sum(len(c.encode()) for c in text if not any(a <= ord(c) <= b for a,b in [(0x3000,0x303F),(0x3040,0x30FF),(0x3400,0x4DBF),(0x4E00,0x9FFF),(0xFF00,0xFFEF)]))
    return {'bytes': len(text.encode()), 'chars': len(text), 'cjkChars': cjk, 'estTokens': cjk + (other + 3)//4}


def _sources(root: Path, slug, payload, skill: str) -> list[dict]:
    from workbench.work_items.store import item_path
    base = item_path(root, slug) if slug else root
    context = payload.get('instructionContext', {})
    rows = [*context.get('rules', []), *context.get('facts', []), *payload.get('sources', [])]
    if not payload.get('selectedTask'):
        rows += [row for row in payload.get('documents', []) if row.get('role') in {'change', 'requirements', 'design'}]
    skill_root = root if (root / '.agents/skills').is_dir() else KIT_ROOT
    rows += [{'path': str(root / 'AGENTS.md')}, {'path': str(skill_root / '.agents/skills' / skill / 'SKILL.md')}]
    rows += [{'path': str(skill_root / '.agents/skills/workspace-verify/references/evidence.md')}] if payload.get('selectedTask') else []
    output, seen = [], set()
    for row in rows:
        raw = row.get('path')
        if not raw:
            continue
        path = Path(raw) if Path(raw).is_absolute() else base / raw
        if not path.is_file() or path.is_symlink():
            continue
        text = path.read_text(encoding='utf-8')
        if 'startLine' in row:
            text = '\n'.join(text.splitlines()[row['startLine'] - 1:row.get('endLine')])
        identity = (str(path), row.get('startLine'), row.get('endLine'))
        if identity in seen:
            continue
        seen.add(identity)
        output.append({'path': str(path), 'startLine': row.get('startLine'), 'endLine': row.get('endLine'),
                       'documentRevision': row.get('documentRevision'), 'scope': row.get('scope'),
                       'estTokens': estimate_tokens(text)['estTokens']})
    return output


def _scenario(root, slug, steps, *, shared_context):
    seen, inputs, savings, command_tokens, source_tokens = set(), [], 0, 0, 0
    for arguments, skill in steps:
        if not shared_context:
            seen.clear()
        result = subprocess.run([sys.executable, '-B', str(KIT_ROOT / 'scripts/kit.py'), 'brief', *arguments, '--root', str(root), '--json'],
                                cwd=root, capture_output=True, text=True, timeout=30)
        payload = json.loads(result.stdout)
        if result.returncode:
            return {'applicable': False, 'reason': payload}
        command_tokens += estimate_tokens(result.stdout)['estTokens']
        for row in _sources(root, slug, payload, skill):
            identity = (row['path'], row['documentRevision'], row['scope'], row['startLine'], row['endLine'])
            reused = identity in seen
            savings += row['estTokens'] if reused else 0
            source_tokens += 0 if reused else row['estTokens']
            inputs.append({**row, 'reused': reused})
            seen.add(identity)
    return {'applicable': True, 'commandEstTokens': command_tokens, 'sourceEstTokens': source_tokens,
            'reusedSourceEstTokens': savings, 'totalEstTokens': command_tokens + source_tokens, 'reads': inputs}


def scenario_report(root: Path, item=None, task=None, repository=None, paths=None):
    from workbench.work_items.query import WorkItemQuery
    result = {name: {'applicable': False, 'reason': '提供 --item 或 --repo 后按实际来源测量'} for name in ['lightweight', 'ordinaryResume', 'threeTasks', 'freshResume']}
    reader = WorkItemQuery(root, item) if item else None
    targets = reader.task_states() if reader else []
    if reader and not repository:
        repository = reader.state['bindings'][0]['repository']
    if repository:
        arguments = ['--repo', repository]
        for path in paths or []:
            arguments += ['--path', path]
        result['lightweight'] = _scenario(root, None, [(arguments, 'workspace-execute-plan')], shared_context=False)
    if reader:
        result['ordinaryResume'] = _scenario(root, item, [([item], 'workspace-execute-plan')], shared_context=False) if reader.state['documentKind'] == 'change' and not targets else {'applicable': False, 'reason': '当前工作项包含独立计划，普通接手场景需使用无计划的 change 工作项'}
        task_id = task or next(iter([row['id'] for row in targets if not row['completed']]), None)
        args = [item, '--task', task_id] if task_id else [item]
        result['freshResume'] = _scenario(root, item, [(args, 'workspace-execute-plan')], shared_context=False)
        if len(targets) >= 3:
            result['threeTasks'] = _scenario(root, item, [([item, '--task', row['id']], 'workspace-execute-plan') for row in targets[:3]], shared_context=True)
        else:
            result['threeTasks'] = {'applicable': False, 'reason': '当前计划不足三个任务'}
    return result


def build_report(root: Path, item=None, task=None, repository=None, paths=None):
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
            'targetMet':total <= 9000 and rules[0]['estTokens'] <= 1200,'commands':commands,
            'scenarios': scenario_report(root, item, task, repository, paths),
            'limitations': '测量 CLI 输出和声明需读取的来源；不含模型生成、历史会话、测试日志、工具内部读取或实际计费。连续任务模拟同会话按路径/版本/作用范围复用，新会话重新读取。'}


def build_parser():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path.cwd());parser.add_argument('--item');parser.add_argument('--task');parser.add_argument('--json',action='store_true')
    parser.add_argument('--repo', dest='repository'); parser.add_argument('--path', dest='paths', action='append')
    return parser


def main(argv=None):
    args=build_parser().parse_args(argv)
    print(json.dumps(build_report(args.root,args.item,args.task,args.repository,args.paths),ensure_ascii=False,indent=None if args.json else 2));return 0

if __name__=='__main__':raise SystemExit(main())
