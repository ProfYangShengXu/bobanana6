import json
with open('_shell/tool-01a15a657473a514fe2dc3b4-65524-9198ce-0196.txt', 'r', encoding='utf-8') as f:
    data = f.read()
    data = data.replace('\\{', '{')
    data = data.replace('\\}', '}')
    json.loads(data)
    print('JSON valid')