import concurrent.futures
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import model_config as config


class ModelConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / '.agents/agents').mkdir(parents=True)
        for role in ('explorer', 'reviewer'):
            (self.root / '.agents/agents' / (role + '.md')).write_text('role')

    def field(self, sid, role='explorer', group='models', harness='codex'):
        return config.resolve(self.root, sid)['roles'][role][group][harness]

    def test_precedence_is_per_field_and_chats_are_isolated(self):
        config.change(self.root, 'explorer', 'models.codex', 'project')
        config.change(self.root, 'explorer', 'effort.codex', 'high')
        config.change(self.root, 'defaults', 'models.codex', 'common', chat=True, sid='a')
        config.change(self.root, 'explorer', 'models.codex', 'specific', chat=True, sid='a')
        self.assertEqual(self.field('a')['value'], 'specific')
        self.assertEqual(self.field('a', 'reviewer')['value'], 'common')
        self.assertEqual(self.field('b')['value'], 'project')
        self.assertEqual(self.field('a', group='effort')['value'], 'high')
        config.change(self.root, 'explorer', 'models.codex', chat=True, sid='a', unset=True)
        self.assertEqual(self.field('a')['value'], 'common')
        config.change(self.root, 'defaults', 'models.codex', 'inherit', chat=True, sid='a')
        self.assertEqual(self.field('a')['value'], 'inherit')

    def test_missing_config_inherits_without_writes(self):
        result = config.resolve(self.root)
        self.assertEqual(self.field(None)['value'], 'inherit')
        self.assertEqual(result['roles']['reviewer']['effort']['claude'], {'value': 'inherit', 'source': 'session'})
        self.assertFalse((self.root / '.agents/state').exists())

    def test_invalid_requests_have_no_side_effects(self):
        for role, key, value, kwargs in [
            ('defaults', 'models.codex', 'x', {}),
            ('unknown', 'models.codex', 'x', {}),
            ('explorer', 'effort.claude', 'high', {}),
            ('explorer', 'models.codex', 'bad model', {}),
            ('explorer', 'effort.codex', 'bad', {}),
            ('explorer', 'models.codex', 'x', {'chat': True}),
            ('explorer', 'models.codex', 'x', {'chat': True, 'sid': '../x'}),
        ]:
            with self.subTest(key=key, value=value, kwargs=kwargs):
                with self.assertRaises(ValueError):
                    config.change(self.root, role, key, value, **kwargs)
        self.assertFalse((self.root / '.agents/state').exists())
        self.assertFalse((self.root / '.agents/config.toml').exists())

    def test_malformed_project_is_not_overwritten(self):
        path = self.root / '.agents/config.toml'
        for text in ('[defaults.models]\ncodex="x"\n', '[agents.explorer.effort]\nclaude="high"\n', '[agents.unknown.models]\ncodex="x"\n', 'bad = ['):
            path.write_text(text)
            with self.assertRaises(ValueError):
                config.change(self.root, 'explorer', 'models.codex', 'x')
            self.assertEqual(path.read_text(), text)
        self.assertFalse((self.root / '.agents/state').exists())

    def test_malformed_chat(self):
        folder = self.root / '.agents/state/chat-config'
        folder.mkdir(parents=True)
        for text in ('[]', '{"agents": 1}', '{"defaults":{"models":{"other":"x"}}}', '{"agents":{},"agents":{}}'):
            (folder / 'a.json').write_text(text)
            with self.assertRaises(ValueError):
                config.load_chat(self.root, 'a', config.roles(self.root))

    def test_symlinks_are_rejected(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.root / '.agents/state').symlink_to(outside)
        with self.assertRaises(ValueError):
            config.change(self.root, 'explorer', 'models.codex', 'x', chat=True, sid='a')
        self.assertEqual(list(outside.iterdir()), [])

    def test_concurrent_updates_preserve_both_fields(self):
        for chat in (True, False):
            with concurrent.futures.ThreadPoolExecutor() as executor:
                futures = [executor.submit(config.change, self.root, role, 'models.codex', role, chat=chat, sid='a') for role in ('explorer', 'reviewer')]
                for future in futures:
                    future.result()
            loaded = config.load_chat(self.root, 'a', config.roles(self.root)) if chat else config.load_project(self.root, config.roles(self.root))
            for role in ('explorer', 'reviewer'):
                self.assertEqual(loaded['agents'][role]['models']['codex'], role)

    def test_project_roundtrip_preserves_other_values_and_escapes(self):
        model = 'vendor/model-"quoted"-\\suffix'
        config.change(self.root, 'reviewer', 'models.claude', model)
        config.change(self.root, 'explorer', 'effort.codex', 'xhigh')
        self.assertEqual(self.field(None, 'reviewer', harness='claude')['value'], model)
        config.change(self.root, 'explorer', 'effort.codex', unset=True)
        self.assertEqual(self.field(None, group='effort')['value'], 'inherit')
        self.assertEqual(self.field(None, 'reviewer', harness='claude')['value'], model)

    def test_config_and_role_file_symlinks_are_rejected(self):
        target = self.root / 'target'
        target.write_text('untouched')
        project = self.root / '.agents/config.toml'
        project.symlink_to(target)
        with self.assertRaises(ValueError):
            config.change(self.root, 'explorer', 'models.codex', 'x')
        self.assertEqual(target.read_text(), 'untouched')
        project.unlink()
        role = self.root / '.agents/agents/explorer.md'
        role.unlink()
        role.symlink_to(target)
        with self.assertRaises(ValueError):
            config.roles(self.root)
        self.assertFalse((self.root / '.agents/state').exists())

    def test_checkout_uses_root_canonical_roles(self):
        (self.root / 'tools').mkdir()
        (self.root / 'tools/installer.py').write_text('')
        (self.root / 'agents').mkdir()
        (self.root / 'agents/custom-role.md').write_text('custom')
        installed = self.root / '.agents/agents'
        installed.rename(self.root / 'saved-roles')
        with patch.object(config, '__file__', str(self.root / 'tools/model_config.py')):
            self.assertEqual(config.roles(self.root), ['custom-role'])

    def test_unrelated_installer_does_not_select_root_roles(self):
        (self.root / 'tools').mkdir()
        (self.root / 'tools/installer.py').write_text('unrelated installer')
        (self.root / 'agents').mkdir()
        (self.root / 'agents/unrelated.md').write_text('unrelated')
        self.assertEqual(config.roles(self.root), ['explorer', 'reviewer'])
        (self.root / '.agents/agents').rename(self.root / 'saved-roles')
        with self.assertRaisesRegex(ValueError, 'No canonical agent roles'):
            config.roles(self.root)

    def test_schema_errors_identify_full_config_path(self):
        project = self.root / '.agents/config.toml'
        project.write_text('[defaults.models]\ncodex="x"\n')
        with self.assertRaises(ValueError) as raised:
            config.load_project(self.root, config.roles(self.root))
        self.assertIn(str(project), str(raised.exception))
        chat = self.root / '.agents/state/chat-config/a.json'
        chat.parent.mkdir(parents=True)
        chat.write_text('{"unexpected": true}')
        with self.assertRaises(ValueError) as raised:
            config.load_chat(self.root, 'a', config.roles(self.root))
        self.assertIn(str(chat), str(raised.exception))

    def test_role_directory_symlink_is_rejected(self):
        installed = self.root / '.agents/agents'
        installed.rename(self.root / 'outside')
        installed.symlink_to(self.root / 'outside')
        with self.assertRaises(ValueError):
            config.roles(self.root)

    def test_empty_role_discovery_requires_explicit_opt_in(self):
        installed = self.root / '.agents/agents'
        installed.rename(self.root / 'saved-roles')
        for create in (False, True):
            if create:
                installed.mkdir()
            with self.assertRaisesRegex(ValueError, 'No canonical agent roles'):
                config.roles(self.root)
            self.assertEqual(config.roles(self.root, allow_empty=True), [])
        installed.rmdir()
        installed.symlink_to(self.root / 'saved-roles')
        with self.assertRaises(ValueError):
            config.roles(self.root, allow_empty=True)


if __name__ == '__main__':
    unittest.main()
