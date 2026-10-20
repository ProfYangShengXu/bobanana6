import os

files = ['output/jd-matrix.md', 'output/interview-bank.md', 'output/roadmap.md', 'output/summary.json',
        'interview-bank.md', 'jd-matrix.md', 'roadmap.md', 'output/dummy.txt', 'part-06.csv']
for f in files:
    if os.path.exists(f):
        print(f'{f}: {os.path.getsize(f)} bytes')
    else:
        print(f'{f}: NOT EXIST')