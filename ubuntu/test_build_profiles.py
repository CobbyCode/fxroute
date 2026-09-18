#!/usr/bin/env python3
"""Exercise ISO actions on fixture trees without building or booting an ISO."""
import contextlib
import hashlib
import importlib.util
import os
import pathlib
import re
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).parent
TRY = '''menuentry "Try or Install Ubuntu" {
    set gfxpayload=keep
    linux  /casper/vmlinuz  --- quiet splash
    initrd /casper/initrd
}
'''
STOCK = 'set timeout=30\nloadfont unicode\n\n' + TRY + '''menuentry "Ubuntu (safe graphics)" {
    linux /casper/vmlinuz nomodeset --- quiet splash
    initrd /casper/initrd
}
grub_platform
if [ "$grub_platform" = "efi" ]; then
menuentry 'Boot from next volume' {
    exit 1
}
menuentry 'UEFI Firmware Settings' {
    fwsetup
}
fi
'''


class ActionContext:
    def __init__(self, root):
        self.root = pathlib.Path(root)
        self.hooks = []

    def p(self, path):
        return str(self.root / path)

    def add_pre_repack_hook(self, hook):
        self.hooks.append(hook)

    def log(self, message):
        pass

    def run(self, command, **kwargs):
        return subprocess.run(command, check=True, **kwargs)


class BuildProfilesTest(unittest.TestCase):
    def run_grub(self, directory, extra=''):
        context = ActionContext(directory)
        path = pathlib.Path(context.p('new/iso/boot/grub/grub.cfg'))
        path.parent.mkdir(parents=True)
        path.write_text(STOCK)
        with patch.dict(os.environ, {'FXROUTE_GRUB_TEST_EXTRA': extra}, clear=True):
            runpy.run_path(str(ROOT / 'livefs-actions/add_install_entry.py'), init_globals={'ctxt': context})
        return path.read_text()

    def test_exact_three_entries_live_renamed_and_profile_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            output = self.run_grub(directory)
        self.assertEqual(re.findall(r'^menuentry [\"\']([^\"\']+)', output, re.M),
                         ['Try FXRoute Live', 'Install FXRoute Desktop', 'Install FXRoute Headless'])
        renamed_try = TRY.replace('menuentry "Try or Install Ubuntu"',
                                  'menuentry "Try FXRoute Live"')
        self.assertTrue(output.startswith('set timeout=30\nloadfont unicode\n\n' + renamed_try))
        for profile in ('desktop', 'headless'):
            block = re.search(r'menuentry "Install FXRoute ' + profile.title() + r'" \{(.*?)\n\}', output, re.S).group(1)
            before, after = block.split('---', 1)
            self.assertIn(' autoinstall ', before)
            self.assertIn(f'subiquity.autoinstallpath=/cdrom/fxroute-seed/{profile}.yaml', before)
            self.assertNotIn('autoinstall', after)
        self.assertNotIn('ds=nocloud', output)

    def test_test_extras_do_not_select_a_seed_for_try(self):
        with tempfile.TemporaryDirectory() as directory:
            output = self.run_grub(directory, 'console=ttyS0 fxroute.live-password=test')
        self.assertEqual(output.count('console=ttyS0'), 3)
        try_block = output.split('menuentry "Install FXRoute')[0]
        self.assertNotIn('autoinstall', try_block)

    def test_checksum_hook_hashes_final_rebuilt_files_and_new_payload(self):
        action = ROOT / 'livefs-actions/final_checksums.py'
        self.assertTrue(action.exists(), 'Missing final checksum hook')
        with tempfile.TemporaryDirectory() as directory:
            context = ActionContext(directory)
            iso = pathlib.Path(context.p('new/iso'))
            (iso / 'casper').mkdir(parents=True)
            (iso / 'boot/grub').mkdir(parents=True)
            (iso / 'md5sum.txt').write_text('stale checksums\n')
            (iso / 'casper/filesystem.squashfs').write_bytes(b'old squashfs')
            (iso / 'casper/initrd').write_bytes(b'old initrd')
            (iso / 'boot/grub/boot.cat').write_bytes(b'xorriso replaces this')
            runpy.run_path(str(action), init_globals={'ctxt': context})
            self.assertEqual((iso / 'md5sum.txt').read_text(), 'stale checksums\n')
            # Upstream EditContext.repack runs hooks in reverse registration order.
            context.add_pre_repack_hook(lambda: (iso / 'casper/filesystem.squashfs').write_bytes(b'rebuilt squashfs'))
            context.add_pre_repack_hook(lambda: (iso / 'casper/initrd').write_bytes(b'rebuilt initrd'))
            (iso / 'fxroute-seed').mkdir()
            (iso / 'fxroute-seed/desktop.yaml').write_text('autoinstall: {version: 1}\n')
            (iso / 'fxroute payload').write_bytes(b'new payload')
            for hook in reversed(context.hooks):
                hook()
            manifest = (iso / 'md5sum.txt').read_text()
            self.assertNotIn('md5sum.txt', manifest)
            self.assertNotIn('boot.cat', manifest)
            self.assertIn(hashlib.md5(b'rebuilt squashfs').hexdigest() + '  ./casper/filesystem.squashfs', manifest)
            self.assertIn(hashlib.md5(b'rebuilt initrd').hexdigest() + '  ./casper/initrd', manifest)
            self.assertIn('./fxroute-seed/desktop.yaml', manifest)
            subprocess.run(['md5sum', '--check', 'md5sum.txt'], cwd=iso, check=True, capture_output=True)
            (iso / 'casper/filesystem.squashfs').write_bytes(b'corrupted')
            result = subprocess.run(['md5sum', '--check', 'md5sum.txt'], cwd=iso, capture_output=True)
            self.assertNotEqual(result.returncode, 0, 'Integrity checks must detect corruption')

    def test_checksum_hook_excludes_generated_catalog_not_similarly_named_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            context = ActionContext(directory)
            iso = pathlib.Path(context.p('new/iso'))
            generated = ('boot.catalog', 'boot/grub/boot.cat',
                         'boot/grub/i386-pc/eltorito.img')
            payload = ('casper/filesystem.squashfs', 'casper/initrd', 'casper/vmlinuz',
                       'boot/grub/grub.cfg', 'EFI/boot/bootx64.efi',
                       'fxroute-seed/desktop.yaml', 'fxroute-seed/headless.yaml',
                       'fxroute-iso/source.tar', 'fxroute-iso/boot.catalog',
                       'fxroute-iso/boot.cat', 'fxroute-iso/eltorito.img',
                       'boot.catalog.backup')
            for relative in generated + payload:
                path = iso / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'original ' + relative.encode())
            runpy.run_path(str(ROOT / 'livefs-actions/final_checksums.py'),
                           init_globals={'ctxt': context})
            for hook in reversed(context.hooks):
                hook()
            records = (iso / 'md5sum.txt').read_text().splitlines()
            filenames = {record.split('  ', 1)[1] for record in records}
            self.assertNotIn('./boot.catalog', filenames)
            self.assertEqual(filenames, {'./' + relative for relative in payload})
            # xorriso changes these structures only after the checksum hook.
            for relative in generated:
                (iso / relative).write_bytes(b'regenerated boot structure')
            result = subprocess.run(['md5sum', '--check', 'md5sum.txt'], cwd=iso,
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for relative in payload:
                with self.subTest(payload=relative):
                    path = iso / relative
                    original = path.read_bytes()
                    path.write_bytes(b'corrupted')
                    result = subprocess.run(['md5sum', '--check', 'md5sum.txt'],
                                            cwd=iso, capture_output=True)
                    self.assertNotEqual(result.returncode, 0,
                                        'Integrity checks must cover ' + relative)
                    path.write_bytes(original)

    @unittest.skipUnless(importlib.util.find_spec('livefs_edit'),
                         'Run with the livefs-edit Python interpreter for upstream lifecycle check')
    def test_installed_livefs_repack_runs_checksum_after_rebuild_before_iso_write(self):
        from livefs_edit.context import EditContext
        with tempfile.TemporaryDirectory() as directory:
            context = ActionContext(directory)
            iso = pathlib.Path(context.p('new/iso'))
            (iso / 'casper').mkdir(parents=True)
            squash = iso / 'casper/filesystem.squashfs'
            squash.write_bytes(b'old')
            runpy.run_path(str(ROOT / 'livefs-actions/final_checksums.py'),
                           init_globals={'ctxt': context})
            context.add_pre_repack_hook(lambda: squash.write_bytes(b'final'))
            stage = pathlib.Path(directory) / 'stage'
            (stage / 'seed').mkdir(parents=True)
            (stage / 'fxroute-iso').mkdir()
            (stage / 'seed/desktop.yaml').write_text('autoinstall: {version: 1}\n')
            (stage / 'fxroute-iso/source.tar').write_bytes(b'fixture payload')
            with patch.dict(os.environ, {'FXROUTE_UBUNTU_STAGE_DIR': str(stage)}):
                runpy.run_path(str(ROOT / 'livefs-actions/cp_payload.py'),
                               init_globals={'ctxt': context})
            self.assertEqual((iso / 'fxroute-seed/desktop.yaml').read_text(),
                             'autoinstall: {version: 1}\n')
            self.assertEqual((iso / 'fxroute-iso/source.tar').read_bytes(), b'fixture payload')
            # Exercise upstream's actual repack method without mounts or ISO
            # creation. Only its external repack_iso boundary is substituted.
            context._pre_repack_hooks = context.hooks
            context.logged = lambda *args: contextlib.nullcontext()
            context._source_overlay = type('ChangedOverlay', (), {'unchanged': lambda self: False})()
            context.source_fstype = 'iso9660'
            def check_manifest_at_iso_write(destination):
                self.assertIn(hashlib.md5(b'final').hexdigest(),
                              (iso / 'md5sum.txt').read_text())
            context.repack_iso = check_manifest_at_iso_write
            self.assertTrue(EditContext.repack(context, 'unused.iso'))

    def test_build_registers_checksum_hook_before_any_rebuild_action(self):
        # The ordering is a CLI boundary contract: no ISO build in unit tests.
        build = (ROOT / 'build-ubuntu-iso.sh').read_text()
        invocation = build[build.index('livefs-edit "$BASE_ISO" "$OUTPUT"'):]
        actions = re.findall(r'livefs-actions/([^" ]+)\.py', invocation)
        # Hook ordering, not CLI position: final_checksums registers a
        # pre-repack hook and must appear before any action that triggers a
        # squashfs/initrd rebuild (remove_cdrom_source etc.).
        rebuilders = {'remove_cdrom_source', 'prep_chroot'}
        first = next(a for a in actions if a in rebuilders)
        self.assertLess(actions.index('final_checksums'), actions.index(first))
        self.assertNotIn('99-fxroute-seed.cfg', invocation)
        self.assertNotIn('--add-autoinstall-config', invocation)
        self.assertIn('prepare-headless-target.sh', build)
        self.assertIn('generate-seeds.py', build)


if __name__ == '__main__':
    unittest.main()
