import json

data = {
    "sources_used": 4,
    "companies_covered": 10,
    "questions_collected": 21,
    "stages": 5,
    "degraded_sources": 0
}

with open("output/summary.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print("summary.json 已创建")