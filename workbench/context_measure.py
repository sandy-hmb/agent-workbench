"""Read-only text volume estimates for actual default workflow entrypoints."""
from __future__ import annotations
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from workbench.resources import KIT_ROOT

DEFAULT_RULES = ['AGENTS.md', *[f'.agents/skills/{name}/SKILL.md' for name in ['workspace-item-design','workspace-writing-plan','workspace-execute-plan','workspace-verify']]]
TASK_REFERENCE_RULES = ['.agents/skills/workspace-verify/references/evidence.md']
RULES = [*DEFAULT_RULES, *TASK_REFERENCE_RULES]
BASELINE = 13561
DEFAULT_RULE_TOKEN_BUDGET = 5200
TASK_REFERENCE_TOKEN_BUDGET = 700
RULE_TOKEN_BUDGET = 5700


def estimate_tokens(text: str) -> dict:
    cjk = sum(any(a <= ord(c) <= b for a, b in [(0x3000,0x303F),(0x3040,0x30FF),(0x3400,0x4DBF),(0x4E00,0x9FFF),(0xFF00,0xFFEF)]) for c in text)
    other = sum(len(c.encode()) for c in text if not any(a <= ord(c) <= b for a,b in [(0x3000,0x303F),(0x3040,0x30FF),(0x3400,0x4DBF),(0x4E00,0x9FFF),(0xFF00,0xFFEF)]))
    return {'bytes': len(text.encode()), 'chars': len(text), 'cjkChars': cjk, 'estTokens': cjk + (other + 3)//4}


def _file_measurement(data: bytes, start_line=None, end_line=None) -> tuple[dict, str]:
    """Measure selected text while keeping a revision for the complete source file."""
    text = data.decode('utf-8')
    if start_line is not None:
        text = '\n'.join(text.splitlines()[start_line - 1:end_line])
    measurement = estimate_tokens(text)
    measurement['bytes'] = len(text.encode('utf-8'))
    revision = 'sha256:' + hashlib.sha256(data).hexdigest()
    return measurement, revision


def _rule_rows(root: Path, paths: list[str]) -> list[dict]:
    rows = []
    for name in paths:
        data = (root / name).read_bytes()
        measurement, revision = _file_measurement(data)
        rows.append({'path': name, 'documentRevision': revision, **measurement})
    return rows


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
        data = path.read_bytes()
        measurement, revision = _file_measurement(data, row.get('startLine'), row.get('endLine'))
        identity = (str(path), row.get('startLine'), row.get('endLine'))
        if identity in seen:
            continue
        seen.add(identity)
        output.append({'path': str(path), 'startLine': row.get('startLine'), 'endLine': row.get('endLine'),
                       'documentRevision': row.get('documentRevision') or revision,
                       'fragmentRevision': row.get('fragmentRevision'), 'scope': row.get('scope'),
                       **measurement})
    return output


def _scenario(root, slug, steps, *, shared_context):
    seen, inputs = set(), []
    savings = source_tokens = source_bytes = 0
    source_read_tokens = source_read_bytes = 0
    command_tokens = command_output_bytes = 0
    command_input_tokens = command_input_bytes = 0
    for arguments, skill in steps:
        if not shared_context:
            seen.clear()
        request = json.dumps(arguments, ensure_ascii=False, separators=(',', ':'))
        request_measurement = estimate_tokens(request)
        command_input_tokens += request_measurement['estTokens']
        command_input_bytes += request_measurement['bytes']
        result = subprocess.run([sys.executable, '-B', str(KIT_ROOT / 'scripts/kit.py'), 'brief', *arguments, '--root', str(root), '--json'],
                                cwd=root, capture_output=True, text=True, timeout=30)
        payload = json.loads(result.stdout)
        if result.returncode:
            return {'applicable': False, 'reason': payload}
        command_measurement = estimate_tokens(result.stdout)
        command_tokens += command_measurement['estTokens']
        command_output_bytes += command_measurement['bytes']
        for row in _sources(root, slug, payload, skill):
            identity = (row['path'], row.get('fragmentRevision') or row['documentRevision'], row['scope'], row['startLine'], row['endLine'])
            reused = identity in seen
            savings += row['estTokens'] if reused else 0
            source_read_tokens += row['estTokens']
            source_read_bytes += row['bytes']
            if reused:
                source_bytes += 0
            else:
                source_bytes += row['bytes']
            source_tokens += 0 if reused else row['estTokens']
            inputs.append({**row, 'reused': reused})
            seen.add(identity)
    return {'applicable': True, 'commandEstTokens': command_tokens, 'sourceEstTokens': source_tokens,
            'reusedSourceEstTokens': savings, 'totalEstTokens': command_tokens + source_tokens,
            'commandInputBytes': command_input_bytes, 'commandInputEstTokens': command_input_tokens,
            'commandOutputBytes': command_output_bytes, 'commandOutputEstTokens': command_tokens,
            'inputBytes': source_read_bytes, 'inputEstTokens': source_read_tokens,
            'outputBytes': command_output_bytes, 'outputEstTokens': command_tokens,
            'sourceBytes': source_bytes, 'sourceReadBytes': source_read_bytes,
            'sourceReadEstTokens': source_read_tokens,
            'reusedSourceBytes': source_read_bytes - source_bytes,
            'usage': {'inputBytes': source_read_bytes, 'inputEstTokens': source_read_tokens,
                      'outputBytes': command_output_bytes, 'outputEstTokens': command_tokens,
                      'reusedInputBytes': source_read_bytes - source_bytes,
                      'reusedInputEstTokens': savings, 'requestCount': len(steps)},
            'reads': inputs}


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
    default_rules = _rule_rows(root, DEFAULT_RULES)
    task_reference_rules = _rule_rows(root, TASK_REFERENCE_RULES)
    rules = [*default_rules, *task_reference_rules]
    queries = [['status','--json']] if item is None else [['brief',item,'--json'], *([['brief',item,'--task',task,'--json']] if task else [])]
    commands = []
    for arguments in queries:
        result = subprocess.run([sys.executable,'-B',str(root/'scripts/kit.py'),*arguments],cwd=root,capture_output=True,text=True,timeout=30)
        request_measurement = estimate_tokens(json.dumps(arguments, ensure_ascii=False, separators=(',', ':')))
        output_measurement = estimate_tokens(result.stdout)
        commands.append({'arguments': arguments, 'exitCode': result.returncode,
                         'inputBytes': request_measurement['bytes'], 'inputEstTokens': request_measurement['estTokens'],
                         'outputBytes': output_measurement['bytes'], 'outputEstTokens': output_measurement['estTokens'],
                         **output_measurement})
    default_total = sum(row['estTokens'] for row in default_rules)
    task_reference_total = sum(row['estTokens'] for row in task_reference_rules)
    total = default_total + task_reference_total
    item_documents = {}
    if item:
        from workbench.work_items.query import WorkItemQuery
        reader = WorkItemQuery(root, item)
        item_documents = {row['path']: estimate_tokens(row['content']) for role in ('change', 'requirements', 'design', 'plan', 'readme')
                          if (row := reader.document(role))}
    return {'schemaVersion':2,'tokenModel':'CJK 1 char/token; other UTF-8 bytes/4 estimate, not billed usage',
            'rules':rules,'defaultRuleEstTokens':default_total,'taskReferenceEstTokens':task_reference_total,
            'totalRuleEstTokens':total,'baselineRuleEstTokens':BASELINE,
            'defaultRuleTokenBudget':DEFAULT_RULE_TOKEN_BUDGET,
            'taskReferenceTokenBudget':TASK_REFERENCE_TOKEN_BUDGET,'ruleTokenBudget':RULE_TOKEN_BUDGET,
            'targetMet':default_total <= DEFAULT_RULE_TOKEN_BUDGET and task_reference_total <= TASK_REFERENCE_TOKEN_BUDGET and total <= RULE_TOKEN_BUDGET and rules[0]['estTokens'] <= 1200,'commands':commands,
            'scenarios': scenario_report(root, item, task, repository, paths),
            'itemDocuments': item_documents, 'softBudgets': {'readme': 1500, 'taskResume': 15000},
            'limitations': '输入/输出字段分别测量声明需读取的来源和 CLI 请求/输出；不含模型生成、历史会话、测试日志、工具内部读取或实际计费。连续任务模拟同会话按路径/版本/作用范围复用，新会话重新读取。报告不落盘正文或凭据。'}


def build_parser():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path.cwd());parser.add_argument('--item');parser.add_argument('--task');parser.add_argument('--json',action='store_true')
    parser.add_argument('--repo', dest='repository'); parser.add_argument('--path', dest='paths', action='append')
    return parser


def main(argv=None):
    args=build_parser().parse_args(argv)
    print(json.dumps(build_report(args.root,args.item,args.task,args.repository,args.paths),ensure_ascii=False,indent=None if args.json else 2));return 0

if __name__=='__main__':raise SystemExit(main())
