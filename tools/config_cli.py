"""Configuration commands: persisted preferences, not a harness control API."""
import argparse
import json
from pathlib import Path
import tomllib

import frontmatter
import model_config as config
import state_fs as fs
import task_state as ts


def add_commands(sub):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--project', type=Path, default=argparse.SUPPRESS)
    common.add_argument('--session-id', default=argparse.SUPPRESS)
    common.add_argument('--chat', action='store_true', default=argparse.SUPPRESS,
                        help='use current chat overrides, independent of task binding')
    parser = sub.add_parser('config', parents=[common], help='model and effort preferences')
    commands = parser.add_subparsers(dest='config_command', required=True)
    show = commands.add_parser('show', parents=[common])
    show.add_argument('--json', action='store_true')
    for name in ('set', 'unset'):
        command = commands.add_parser(name, parents=[common])
        command.add_argument('role', help='canonical role, or defaults with --chat')
        command.add_argument('key', help='models.claude, models.codex, or effort.codex')
        if name == 'set':
            command.add_argument('value')


def native_warnings(root, roles):
    """Surface pins that can defeat launch preferences without editing role files."""
    warnings = []
    for role in roles:
        for harness, directory, suffix in (('claude', '.claude/agents', 'md'),
                                           ('codex', '.codex/agents', 'toml')):
            path = Path(directory) / f'{role}.{suffix}'
            raw = fs.read(root, path)
            if raw is None:
                warnings.append(f'{path}: native role missing; generate or update before delegation')
                continue
            text = raw.decode('utf-8')
            data = (tomllib.loads(text) if harness == 'codex' else
                    frontmatter.parse(frontmatter.split(text, str(path))[0], str(path)))
            keys = ('model', 'model_reasoning_effort') if harness == 'codex' else ('model', 'effort')
            for key in keys:
                if key in data and not (key == 'model' and data[key] == 'inherit'):
                    warnings.append(f'{path}: {key} is pinned to {data[key]!r}; '
                                    'migrate the canonical definition and regenerate before relying on inheritance')
    return warnings


def run(args, root):
    chat = getattr(args, 'chat', False)
    explicit = getattr(args, 'session_id', None)
    sid = (explicit if explicit is not None else ts.session_id()) if chat else None
    if chat and sid is None:
        raise config.ConfigError('Chat configuration requires a session ID; set AGENTS_SESSION_ID or --session-id')
    if sid is not None:
        ts.validate_session(sid)
    if args.config_command != 'show':
        config.change(root, args.role, args.key, getattr(args, 'value', None),
                      chat=chat, sid=sid, unset=args.config_command == 'unset')
        path = config.CHAT / f'{sid}.json' if chat else config.PROJECT
        print(f'Saved: {path}. Applies through the orchestrator on subsequent launches; no update needed.')
        return 0
    result = config.resolve(root, sid)
    result['warnings'] = native_warnings(root, result['roles'])
    result['warnings'].append('Runtime model availability is not checked. Inherit delegates to harness resolution; '
                              'native defaults may override the parent. Pass the parent values explicitly '
                              'when exact inheritance is required and the tool supports it.')
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f'Configuration: {"chat " + sid if chat else "project"} ({result["application"]})')
        for role, settings in result['roles'].items():
            for group, values in settings.items():
                for harness, selection in values.items():
                    print(f'{role:16} {group}.{harness:7} = {selection["value"]} [{selection["source"]}]')
        for warning in result['warnings']:
            print(f'WARN: {warning}')
    return 0
