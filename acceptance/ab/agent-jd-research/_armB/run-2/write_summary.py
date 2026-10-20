import json

data = {
    "sources_used": 1,
    "companies_covered": 8,
    "questions_collected": 36,
    "stages": 5,
    "degraded_sources": 2
}

with open('summary.json', 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False)

print("Written summary.json successfully")