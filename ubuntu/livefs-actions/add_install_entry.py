"""livefs-edit --python action: GRUB entries for the FXRoute ISO.

Duplicates the Try or Install Ubuntu menuentry as Install FXRoute with
autoinstall on the installer kernel args (before ---, installer-only).
If FXROUTE_GRUB_TRY_EXTRA is set (QEMU test builds only), those args are
appended to the Try entry linux line at build time instead of being typed
at boot. Every other entry stays byte-identical.
"""
import os
import re

path = ctxt.p('new/iso/boot/grub/grub.cfg')
with open(path) as fp:
    text = fp.read()

pattern = re.compile(
    r'menuentry "Try or Install Ubuntu" \{\n'
    r'(?:[^\n]*\n)*?\}\n',
)
match = pattern.search(text)
if not match:
    raise Exception('Try or Install Ubuntu menuentry not found in grub.cfg')
if 'menuentry "Install FXRoute"' in text:
    raise Exception('Install FXRoute menuentry already present')

entry = match.group(0)
lines = []
for line in entry.splitlines(keepends=True):
    stripped = line.strip()
    if stripped.startswith('linux '):
        if 'autoinstall' not in stripped.split('---')[0].split():
            line = line.replace(' --- ', ' autoinstall --- ', 1)
    lines.append(line)
new_entry = ''.join(lines).replace(
    'menuentry "Try or Install Ubuntu"',
    'menuentry "Install FXRoute"',
    1,
)

text = text[:match.end()] + new_entry + text[match.end():]

extra = os.environ.get('FXROUTE_GRUB_TRY_EXTRA', '').strip()
if extra:
    def add_extra(m):
        out = []
        for line in m.group(0).splitlines(keepends=True):
            if line.strip().startswith('linux ') and ' --- ' in line:
                line = line.replace(' --- ', ' ' + extra + ' --- ', 1)
            out.append(line)
        return ''.join(out)
    text, count = pattern.subn(add_extra, text, count=1)
    if count != 1:
        raise Exception('Try entry not found for kernel extras')
    print('appended kernel extras to Try entry: ' + extra)

with open(path, 'w') as fp:
    fp.write(text)
print('added Install FXRoute GRUB entry')
