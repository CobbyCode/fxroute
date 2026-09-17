"""livefs-edit --python action: add an "Install FXRoute" GRUB entry.

Duplicates the "Try or Install Ubuntu" menuentry, appends `autoinstall` to
the installer kernel args (before `---`, installer-only) and keeps every
other entry byte-identical. The default boot path (Try) is untouched.
"""
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
with open(path, 'w') as fp:
    fp.write(text)
print('added Install FXRoute GRUB entry')
