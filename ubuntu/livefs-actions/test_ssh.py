"""livefs-edit --python action: enable the QEMU test SSH hook (test builds).

Installs fxroute-test-ssh.service + helper and enables the unit via a
wants-symlink. Only wired up when FXROUTE_TEST_SSH=1 (test-seed builds).
"""
import os

if os.environ.get('FXROUTE_TEST_SSH', '') != '1':
    print('test SSH hook not requested, skipping')
else:
    base = ctxt.edit_squashfs(get_squash_names(ctxt)[0])
    unit = base + '/etc/systemd/system/fxroute-test-ssh.service'
    if not os.path.isfile(unit):
        raise Exception('test SSH unit missing: copy it before this action')
    wants = base + '/etc/systemd/system/multi-user.target.wants'
    os.makedirs(wants, exist_ok=True)
    link = os.path.join(wants, 'fxroute-test-ssh.service')
    if os.path.islink(link) or os.path.exists(link):
        os.remove(link)
    os.symlink('../fxroute-test-ssh.service', link)
    print('test SSH hook enabled')
