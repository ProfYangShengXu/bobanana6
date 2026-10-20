# 解析 hunt:1 文件，提取 JD 信息

# 读取文件 content
with open('hunt1_complete.txt', 'r', encoding='utf-8') as f:
    content = f.read()

# 提取所有公司名（在 resume 或 JD 部分出现的公司）
companies = ['字节跳动', '腾讯', '阿里巴巴', '百度', '湖南林泽', '腾讯混元', '月之暗面', 
             '智谱 AI', 'DeepSeek', 'MiniMax', '商汤', '京东', '美团', '快手', '小红书', '蚂蚁集团',
             '华为', '微软', 'OpenAI', 'Anthropic', 'Microsoft', 'Palantir']

# 提取 JD 相关行
jd_lines = []
in_jd_section = False
for i, line in enumerate(content.split('\n')):
    if 'JD' in line and '####' in line:
        jd_lines.append((i, line.strip()))
        in_jd_section = True
    elif line.startswith('### ') or line.startswith('#### '):
        in_jd_section = False
    elif in_jd_section:
        jd_lines.append((i, line.strip()))

print(f"找到 {len(jd_lines)} 行可能与 JD 相关")
print("前 10 行:")
for idx, text in jd_lines[:10]:
    print(f"  {idx}: {text[:100]}")

# 提取公司名
found_companies = set()
for idx, line in jd_lines:
    for comp in companies:
        if comp in line:
            found_companies.add(comp)

print(f"\n找到的公司：{found_companies}")

# 提取薪资信息
salary_lines = []
for i, line in enumerate(content.split('\n')):
    if '薪' in line.lower() or 'k' in line.lower():
        salary_lines.append((i, line.strip()[:100]))

print(f"\n薪资行：{len(salary_lines)}")
print("前三行:")
for idx, text in salary_lines[:5]:
    print(f"  {idx}: {text}")