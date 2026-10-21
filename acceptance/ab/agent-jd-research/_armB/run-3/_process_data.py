import json

# 读取四个来源的完整数据
sources = ['hunt-interview:3', 'hunt-interview:4', 'hunt-interview:5', 'hunt-interview:6']

all_questions = []

for ref in sources:
    with open(ref.replace(':', '_').lower() + '.txt', 'r', encoding='utf-8') as f:
        try:
            data = json.load(f)
            print(f"\n{ref} - Total lines: {data['total_lines'] if 'total_lines' in data else 'N/A'}")
            
            # 只处理那些包含实际内容的部分
            if 'lines' in data:
                for line_data in data['lines']:
                    all_questions.append((line_data['line'], line_data['text'], ref))
            
            if 'matches' in data:
                print(f"  Matches: {len(data['matches'])}")
        except Exception as e:
            print(f"Error reading {ref}: {e}")