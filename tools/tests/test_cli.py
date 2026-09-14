"""End-to-end installer tests, never modifying the source checkout or real memory."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'tools'))
import installer
from install_support import Conflict


def run_git(root, *args):
    return subprocess.run(['git','-C',str(root),'-c','user.name=test','-c','user.email=test@local',*args],
                          check=True,capture_output=True,text=True).stdout.strip()


def tree(root):
    result = {}
    for p in root.rglob('*'):
        if '.git' in p.relative_to(root).parts:
            continue
        if p.is_symlink(): result[str(p.relative_to(root))] = ('link',os.readlink(p))
        elif p.is_file(): result[str(p.relative_to(root))] = p.read_bytes()
    return result


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base/'target'
        self.repo.mkdir()
        run_git(self.repo,'init','-q','-b','main')
        (self.repo/'app.txt').write_text('existing app')
        run_git(self.repo,'add','app.txt');run_git(self.repo,'commit','-qm','initial')
        self.source = self.base/'tool'
        shutil.copytree(ROOT,self.source,symlinks=True,ignore=shutil.ignore_patterns('.git','memory','__pycache__'))
        self.env = dict(os.environ, AGENTS_MEMORY_STORE=str(self.base/'store'),
                        PYTHONDONTWRITEBYTECODE='1')

    def cli(self, *args, cwd=None, code=0, env=None):
        r = subprocess.run([str(self.source/'bin/agent-system'),*args],cwd=cwd or self.repo,
                           env=env or self.env,capture_output=True,text=True)
        self.assertEqual(r.returncode,code,r.stdout+r.stderr)
        return r

    def install(self):
        return self.cli('init')

    def prepare(self):
        source=self.repo/'.agents/memory';source.mkdir(parents=True)
        (source/'MEMORY.md').write_text('Tests need a fixture. Cache has a size limit.')
        package=self.base/'prepared'
        script=self.source/'skills/migrate-memory/scripts/prepare.py'
        subprocess.run([sys.executable,str(script),'snapshot',str(source),str(package)],check=True,capture_output=True,env=self.env)
        (package/'facts/tests.md').write_text('Tests need a fixture.')
        (package/'facts/cache.md').write_text('Cache has a size limit.')
        (package/'mapping.json').write_text(json.dumps({'MEMORY.md':['tests.md','cache.md']}))
        (package/'report.md').write_text('Record 1 -> tests.md; record 2 -> cache.md; no omissions.')
        subprocess.run([sys.executable,str(script),'seal',str(source),str(package)],check=True,capture_output=True,env=self.env)
        return package

    def test_init_idempotent_from_subdirectory_and_doctor(self):
        source_before=tree(self.source)
        d=self.repo/'nested';d.mkdir()
        self.cli('init',cwd=d)
        before=tree(self.repo); cfg=(self.repo/'.git/config').read_bytes()
        self.cli('init');self.cli('doctor')
        self.assertEqual(before,tree(self.repo))
        self.assertEqual(cfg,(self.repo/'.git/config').read_bytes())
        self.assertEqual(source_before,tree(self.source))
        self.assertEqual(run_git(self.repo,'rev-list','--count','HEAD'),'1')
        self.assertFalse((self.repo/'tools').exists())

    def test_only_distributable_skills_are_installed_and_updated(self):
        local = self.source/'.agents/skills'
        shutil.rmtree(local)
        private = local/'private-helper'
        private.mkdir(parents=True)
        (private/'SKILL.md').write_text('local development only')
        # Finder metadata is not a distributable skill.
        (self.source/'skills/.DS_Store').write_bytes(b'finder metadata')
        self.install()
        self.cli('doctor')
        target = self.repo/'.agents/skills'
        self.assertFalse((target/'private-helper').exists())
        self.assertFalse((target/'.DS_Store').exists())
        for name in ('checkpoint', 'migrate-memory'):
            expected = {path: data for path, data in tree(self.source/'skills'/name).items()
                        if '.DS_Store' not in Path(path).parts}
            self.assertEqual(expected, tree(target/name))
        src = self.source/'skills/checkpoint/SKILL.md'
        src.write_text(src.read_text()+'\nUpdated public checkpoint instruction.\n')
        self.cli('update')
        self.cli('doctor')
        self.assertEqual(src.read_bytes(), (target/'checkpoint/SKILL.md').read_bytes())
        self.assertFalse((target/'private-helper').exists())

    def test_memory_is_project_repository_without_store_git(self):
        self.install()
        target = self.base/'store/target-memory'
        self.assertFalse((self.base/'store/.git').exists())
        self.assertTrue((target/'.git').is_dir())
        self.assertEqual(run_git(target, 'rev-parse', '--show-toplevel'), str(target))
        self.assertEqual(run_git(target, 'remote'), '')
        r = subprocess.run(['git', '-C', str(target), 'rev-parse', '--verify', 'HEAD'], capture_output=True)
        self.assertNotEqual(r.returncode, 0)  # CLI does not commit memory.

    def test_legacy_shared_store_blocks_init_and_dry_run(self):
        store = self.base/'store';store.mkdir()
        run_git(store, 'init', '-q')
        (store/'fact.md').write_text('preserve')
        before = tree(self.repo)
        cfg = (self.repo/'.git/config').read_bytes()
        for args in [('init', '--dry-run'), ('init',)]:
            out = self.cli(*args, code=1)
            self.assertIn('Legacy shared Git memory store', out.stderr)
            self.assertEqual(tree(self.repo), before)
            self.assertEqual((self.repo/'.git/config').read_bytes(), cfg)
            self.assertFalse((store/'target-memory').exists())
            self.assertTrue((store/'.git').is_dir())

    def test_memory_layout_is_rechecked_before_apply(self):
        with patch.dict(os.environ, self.env):
            plan = installer.make_plan(self.repo, self.source, 'init')
            store = self.base/'store';store.mkdir()
            run_git(store, 'init', '-q')
            with self.assertRaises(Conflict):
                installer.apply_plan(plan)
        self.assertFalse(installer.pending_path(self.repo).exists())
        self.assertFalse((self.repo/installer.MANIFEST).exists())

    def test_finished_task_survives_init_update_and_is_not_selected(self):
        task = self.repo/'.agents/state/tasks/finished';task.mkdir(parents=True)
        (task/'task.md').write_text('---\nid: finished\nstatus: done\nbranch: main\n---\n')
        (task/'journal.md').write_text('Logs to revisit later.\n')
        before = tree(task)
        self.install()
        src = self.source/'agents/reviewer.md'
        src.write_text(src.read_text()+'\nExtra instruction.\n')
        self.cli('update')
        out = self.cli('doctor')
        self.assertEqual(tree(task), before)
        self.assertFalse((self.repo/'.agents/state/ACTIVE').exists())
        self.assertNotIn('активная задача: finished', out.stdout)
        tracked = subprocess.run(['git', '-C', str(self.repo), 'check-ignore', str(task/'journal.md')], capture_output=True)
        self.assertEqual(tracked.returncode, 1)

    def test_preserves_shared_rules_and_foreign_skills(self):
        (self.repo/'AGENTS.md').write_bytes(b'Existing rules\r\n')
        (self.repo/'CLAUDE.md').write_bytes(b'My Claude instructions')
        other=self.repo/'.claude/skills/custom';other.mkdir(parents=True)
        (other/'SKILL.md').write_text('custom skill')
        self.install()
        self.assertTrue((self.repo/'AGENTS.md').read_bytes().startswith(b'Existing rules\r\n'))
        self.assertEqual((other/'SKILL.md').read_text(),'custom skill')
        self.assertTrue((self.repo/'.claude/skills/checkpoint').is_symlink())
        self.cli('doctor')

    def test_adopts_existing_shared_skill_link_without_ownership(self):
        (self.repo/'.agents/skills').mkdir(parents=True)
        (self.repo/'.claude').mkdir()
        (self.repo/'.claude/skills').symlink_to('../.agents/skills')
        self.install(); self.cli('doctor')
        m=json.loads((self.repo/installer.MANIFEST).read_text())
        self.assertNotIn('.claude/skills',m['links'])

    def test_conflict_causes_no_writes_or_config_changes(self):
        conflict=self.repo/'.agents/skills/checkpoint';conflict.mkdir(parents=True)
        (conflict/'notes').write_text('mine')
        before=tree(self.repo);cfg=(self.repo/'.git/config').read_bytes()
        self.cli('init',code=1)
        self.assertEqual(before,tree(self.repo));self.assertEqual(cfg,(self.repo/'.git/config').read_bytes())
        self.assertFalse((self.base/'store').exists())

    def test_native_role_collision_is_not_overwritten(self):
        f=self.repo/'.codex/agents/reviewer.toml';f.parent.mkdir(parents=True);f.write_text('mine')
        self.cli('init',code=1);self.assertEqual(f.read_text(),'mine')
        self.assertFalse((self.base/'store').exists())

    def test_symlink_parent_rejected(self):
        outside=self.base/'outside';outside.mkdir()
        (self.repo/'.agents').symlink_to(outside)
        self.cli('init',code=1);self.assertEqual(list(outside.iterdir()),[])

    def test_runtime_name_collision_under_different_filename(self):
        f=self.repo/'.codex/agents/my-agent.toml';f.parent.mkdir(parents=True)
        f.write_text('name = "reviewer"\ndescription = "mine"\ndeveloper_instructions = "mine"\n')
        self.cli('init',code=1)
        self.assertFalse((self.base/'store').exists())

    def test_doctor_ignores_foreign_canonical_format(self):
        f=self.repo/'.agents/agents/custom.md';f.parent.mkdir(parents=True)
        f.write_text('my unrelated agent, different schema')
        self.install();self.cli('doctor')
        self.assertEqual(f.read_text(),'my unrelated agent, different schema')

    def test_dry_run_has_no_side_effects(self):
        before=tree(self.repo);cfg=(self.repo/'.git/config').read_bytes()
        self.cli('init','--dry-run')
        self.assertEqual(before,tree(self.repo));self.assertEqual(cfg,(self.repo/'.git/config').read_bytes())
        self.assertFalse((self.base/'store').exists())
        self.assertFalse((self.repo/'.git/agent-system-backups').exists())

    def test_update_preserves_user_text_and_updates_bundle(self):
        self.install()
        f=self.repo/'AGENTS.md';f.write_bytes(b'new user rule\n'+f.read_bytes()+b'\nuser footer')
        src=self.source/'agents/reviewer.md';src.write_text(src.read_text()+'\nNew review instruction.\n')
        self.cli('init',code=1)
        self.cli('update','--dry-run')
        self.assertNotIn('New review instruction',(self.repo/'.agents/agents/reviewer.md').read_text())
        self.cli('update');self.cli('doctor')
        self.assertTrue(f.read_bytes().startswith(b'new user rule\n'))
        self.assertTrue(f.read_bytes().endswith(b'user footer'))
        self.assertIn('New review instruction',(self.repo/'.codex/agents/reviewer.toml').read_text())

    def test_local_edits_block_update(self):
        self.install()
        f=self.repo/'.agents/skills/checkpoint/SKILL.md';f.write_text(f.read_text()+'\nlocal edit')
        before=tree(self.repo)
        self.cli('update',code=1);self.cli('doctor',code=1)
        self.assertEqual(before,tree(self.repo))

    def test_update_removes_only_owned_orphans(self):
        self.install()
        foreign=self.repo/'.codex/agents/foreign.toml';foreign.write_text('mine')
        (self.source/'agents/reviewer.md').unlink()
        self.cli('update')
        self.assertFalse((self.repo/'.codex/agents/reviewer.toml').exists())
        self.assertEqual(foreign.read_text(),'mine')

    def test_worktree_recreates_local_memory_with_saved_store(self):
        self.cli('init','--memory-key','project-key')
        run_git(self.repo,'add','.agents','.claude','.codex','AGENTS.md','CLAUDE.md','.gitignore')
        run_git(self.repo,'commit','-qm','install')
        wt=self.base/'worktree';run_git(self.repo,'worktree','add','-q','-b','feature',str(wt))
        env=dict(self.env);env.pop('AGENTS_MEMORY_STORE')
        self.cli('init',cwd=wt,env=env)
        self.assertEqual((wt/'.agents/memory').resolve(),(self.repo/'.agents/memory').resolve())
        self.cli('doctor',cwd=wt,env=env)

    def test_migration_requires_explicit_preparation(self):
        m=self.repo/'.agents/memory';m.mkdir(parents=True);(m/'old.md').write_text('old')
        before=tree(self.repo)
        out=self.cli('init',code=1)
        self.assertIn('migrate-memory/SKILL.md',out.stderr)
        self.assertEqual(before,tree(self.repo))

    def test_migration_preserves_legacy_and_copies_facts(self):
        package=self.prepare()
        original=(self.repo/'.agents/memory/MEMORY.md').read_bytes()
        self.cli('init','--memory-from',str(package),'--dry-run')
        self.assertFalse((self.repo/'.agents/memory').is_symlink())
        self.cli('init','--memory-from',str(package))
        self.assertTrue((self.repo/'.agents/memory').is_symlink())
        self.assertEqual((self.repo/'.agents/memory/tests.md').read_text(),'Tests need a fixture.')
        backups=list((self.repo/'.git/agent-system-backups').glob('*/legacy-original/MEMORY.md'))
        self.assertEqual(len(backups),1);self.assertEqual(backups[0].read_bytes(),original)
        self.assertFalse((self.repo/'.agents/memory/report.md').exists())

    def test_changed_source_blocks_migration(self):
        package=self.prepare();(self.repo/'.agents/memory/MEMORY.md').write_text('changed')
        self.cli('init','--memory-from',str(package),code=1)
        self.assertFalse((self.base/'store').exists())

    def test_destination_conflict_blocks_migration(self):
        package=self.prepare();dest=self.base/'store/target-memory';dest.mkdir(parents=True)
        (dest/'tests.md').write_text('different')
        self.cli('init','--memory-from',str(package),code=1)
        self.assertFalse((self.repo/'.agents/memory').is_symlink())
        self.assertFalse((self.repo/installer.MANIFEST).exists())

    def test_existing_valid_memory_link_is_preserved(self):
        dest=self.base/'custom/my-key';dest.mkdir(parents=True)
        (self.repo/'.agents').mkdir();(self.repo/'.agents/memory').symlink_to(dest)
        env=dict(self.env);env.pop('AGENTS_MEMORY_STORE')
        self.cli('init',env=env)
        self.assertEqual((self.repo/'.agents/memory').resolve(),dest)

    def test_interrupt_leaves_backup_and_blocks_retry(self):
        with patch.dict(os.environ,self.env):
            plan=installer.make_plan(self.repo,self.source,'init')
            with patch.object(installer,'atomic_write',side_effect=OSError('injected failure')):
                with self.assertRaises(OSError): installer.apply_plan(plan)
        self.assertTrue(installer.pending_path(self.repo).exists())
        self.cli('init',code=1);self.cli('doctor',code=1)
        self.assertEqual((self.repo/'app.txt').read_text(),'existing app')

    def test_memory_copy_failure_preserves_source(self):
        package=self.prepare()
        with patch.dict(os.environ,self.env):
            plan=installer.make_plan(self.repo,self.source,'init',memory_from=package)
            real=installer.atomic_write
            def fail(path,data):
                if path.name=='tests.md': raise OSError('copy failed')
                return real(path,data)
            with patch.object(installer,'atomic_write',side_effect=fail):
                with self.assertRaises(OSError): installer.apply_plan(plan)
        self.assertFalse((self.repo/'.agents/memory').is_symlink())
        self.assertTrue((self.repo/'.agents/memory/MEMORY.md').exists())
        self.assertTrue(installer.pending_path(self.repo).exists())

    def test_changed_file_after_plan_is_not_overwritten(self):
        with patch.dict(os.environ,self.env):
            plan=installer.make_plan(self.repo,self.source,'init')
            (self.repo/'AGENTS.md').write_text('concurrent edit')
            with self.assertRaises(Conflict): installer.apply_plan(plan)
        self.assertEqual((self.repo/'AGENTS.md').read_text(),'concurrent edit')
        self.assertFalse(installer.pending_path(self.repo).exists())


    # --- задачи и привязки чатов -------------------------------------------

    def chat(self, sid):
        return dict(self.env, AGENTS_SESSION_ID=sid)

    def ignored(self, name):
        r = subprocess.run(['git','-C',str(self.repo),'check-ignore',str(self.repo/name)],
                           capture_output=True)
        return r.returncode == 0

    def test_gitignore_block_covers_bindings_and_not_task_state(self):
        self.install()
        block = (self.repo/'.gitignore').read_text()
        self.assertIn('/.agents/state/sessions/', block)
        self.assertIn('/.agents/state/ACTIVE', block)
        self.assertIn('/.agents/state/LOCK', block)
        (self.repo/'.agents/state/sessions').mkdir(parents=True, exist_ok=True)
        (self.repo/'.agents/state/sessions/sid-1').write_text('{}')
        self.assertTrue(self.ignored('.agents/state/sessions/sid-1'))
        self.assertFalse(self.ignored('.agents/state/tasks'))

    def test_task_lifecycle_through_the_cli(self):
        self.install()
        self.cli('task','new','task-a','--branch','feature-a')
        task = self.repo/'.agents/state/tasks/task-a'
        self.assertIn('id: task-a', (task/'task.md').read_text())

        # Единственный кандидат предлагается, но молча не привязывается.
        out = self.cli('task','status',env=self.chat('chat-one'))
        self.assertIn('task bind task-a', out.stdout)
        self.assertFalse((self.repo/'.agents/state/sessions/chat-one').exists())

        self.cli('task','bind','task-a',env=self.chat('chat-one'))
        record = json.loads((self.repo/'.agents/state/sessions/chat-one').read_text())
        self.assertEqual(record['slug'], 'task-a')

        # Два чата на одной задаче — норма, и записи журнала не сталкиваются.
        self.cli('task','bind','task-a',env=self.chat('chat-two'))
        for sid in ('chat-one','chat-two'):
            self.cli('task','checkpoint','--message',f'запись {sid}',env=self.chat(sid))
        entries = sorted(p.name for p in (task/'journal').iterdir())
        self.assertEqual(len(entries), 2)
        self.assertEqual(len(set(entries)), 2)

        out = self.cli('task','list',env=self.chat('chat-one'))
        self.assertIn('chat-one, chat-two', out.stdout)
        self.cli('doctor')

    def test_journal_reader_handles_bound_explicit_and_legacy_entries(self):
        self.install()
        self.cli('task', 'new', 'reading')
        self.cli('task', 'bind', 'reading', env=self.chat('short'))
        task = self.repo/'.agents/state/tasks/reading'
        (task/'journal.md').write_text('LEGACY_DECISION\n')
        for n in (1, 2, 10):
            suffix = '' if n == 1 else f'-{n}'
            (task/'journal'/f'20260910T120000Z-short{suffix}.md').write_text(
                f'---\nsession: short\nat: 2026-09-10T12:00:00Z\n---\nOLD_{n}\n')
        self.cli('task', 'checkpoint', '--message', 'NEW_DECISION', env=self.chat('short'))
        bound = self.cli('task', 'journal', env=self.chat('short')).stdout
        explicit = self.cli('task', 'journal', 'reading', env=self.chat('unbound')).stdout
        self.assertEqual(bound, explicit)
        positions = [bound.index(word) for word in ('LEGACY_DECISION', 'OLD_1\n', 'OLD_2\n', 'OLD_10\n', 'NEW_DECISION')]
        self.assertEqual(positions, sorted(positions))
        before = tree(task)
        self.cli('task', 'journal', 'reading')
        self.assertEqual(tree(task), before)

    def test_corrupt_journal_fails_reader_and_doctor(self):
        self.install()
        self.cli('task', 'new', 'reading')
        self.cli('task', 'checkpoint', 'reading', '--message', 'VALID_SECRET_DECISION', env=self.chat('sid'))
        (self.repo/'.agents/state/tasks/reading/journal/broken.md').write_text('partial')
        out = self.cli('task', 'journal', 'reading', code=2)
        self.assertNotIn('VALID_SECRET_DECISION', out.stdout)
        self.assertIn('broken.md', out.stderr)
        self.cli('doctor', code=1)

    def test_explicit_checkpoint_rejects_missing_done_and_invalid_stage(self):
        self.install()
        self.cli('task', 'new', 'finished')
        self.cli('task', 'set-status', 'finished', 'done')
        for slug in ('missing', 'finished'):
            self.cli('task', 'checkpoint', slug, '--message', 'not saved', env=self.chat('sid'), code=2)
        self.assertFalse((self.repo/'.agents/state/tasks/missing').exists())
        self.assertEqual(list((self.repo/'.agents/state/tasks/finished/journal').iterdir()), [])
        self.cli('task', 'set-status', 'finished', 'active')
        self.cli('task', 'checkpoint', 'finished', '--stage', 'a\nsession: replacement',
                 '--message', 'not saved', env=self.chat('sid'), code=2)
        self.assertEqual(list((self.repo/'.agents/state/tasks/finished/journal').iterdir()), [])
        self.cli('task', 'checkpoint', 'finished', '--message', 'saved', env=self.chat('sid'))

    def test_rebinding_another_task_requires_an_explicit_command(self):
        self.install()
        self.cli('task','new','task-a')
        self.cli('task','new','task-b')
        self.cli('task','bind','task-a',env=self.chat('chat-one'))
        out = self.cli('task','bind','task-b',env=self.chat('chat-one'),code=2)
        self.assertIn('--force', out.stderr)
        self.cli('task','bind','task-b','--force',env=self.chat('chat-one'))
        record = json.loads((self.repo/'.agents/state/sessions/chat-one').read_text())
        self.assertEqual(record['slug'], 'task-b')

    def test_finished_task_keeps_its_journal_and_reports_stale_chats(self):
        self.install()
        self.cli('task','new','task-a')
        self.cli('task','bind','task-a',env=self.chat('chat-one'))
        self.cli('task','checkpoint','--message','работа сделана',env=self.chat('chat-one'))
        out = self.cli('task','set-status','task-a','done')
        self.assertIn('chat-one', out.stdout)
        self.cli('doctor',code=1)                     # привязка к завершённой задаче
        self.cli('task','unbind',env=self.chat('chat-one'))
        self.cli('doctor')
        self.assertEqual(len(list((self.repo/'.agents/state/tasks/task-a/journal').iterdir())), 1)

    def test_legacy_pointer_is_migrated_on_first_bind(self):
        self.install()
        self.cli('task','new','task-a')
        state = self.repo/'.agents/state'
        (state/'ACTIVE').write_text('task-a\n')
        (state/'LOCK').write_text('claude\n')
        out = self.cli('task','status',env=self.chat('chat-one'))
        self.assertIn('LEGACY', out.stdout)
        self.cli('task','bind',env=self.chat('chat-one'))   # без slug: берётся из ACTIVE
        self.assertTrue((state/'ACTIVE').exists())
        self.assertTrue((state/'LOCK').exists())
        # Owner confirmed stopped: manual cleanup, then matching bind migrates ACTIVE.
        (state/'LOCK').unlink()
        self.cli('task','bind',env=self.chat('chat-one'))
        self.assertFalse((state/'ACTIVE').exists())
        record = json.loads((state/'sessions/chat-one').read_text())
        self.assertEqual(record['slug'], 'task-a')

    def test_invalid_session_id_is_refused(self):
        self.install()
        self.cli('task','new','task-a')
        self.cli('task','bind','task-a',env=self.chat('../escape'),code=2)
        self.assertFalse((self.repo/'.agents/state/sessions').exists())


if __name__=='__main__': unittest.main()
