from pathlib import Path
from collections import Counter
text = Path('full_backend_suite3.log').read_text(errors='replace')
failed_lines = [line.strip() for line in text.splitlines() if line.startswith('FAILED ')]
print('Total failed lines:', len(failed_lines))
modules = [line.split()[1].split('::')[0] for line in failed_lines]
for mod, count in Counter(modules).most_common(60):
    print(f'{count:4d} {mod}')
print('\nAccount-related fails:')
for line in failed_lines:
    if any(k in line.lower() for k in ['account','accounts','tenant','duplicate','export']):
        print(line)
