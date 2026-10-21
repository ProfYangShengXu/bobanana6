import json
d = json.load(open('summary.json'))
print(f'Files created:')
print(f'- jd-matrix.md: {d["companies_covered"]} companies')
print(f'- interview-bank.md: {d["questions_collected"]} questions')
print(f'- roadmap.md: {d["stages"]} stages')