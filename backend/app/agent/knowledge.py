"""Small versioned local knowledge retrieval; never a balance/transaction source."""
import json
import re
from pathlib import Path

CATALOG = Path(__file__).with_name('knowledge-v1.json')


def terms(text):
    normalized = re.sub(r'\s+', '', text.lower())
    return set(re.findall(r'[a-z0-9_]+', normalized)) | {normalized[i:i+2] for i in range(len(normalized)-1)}


def search(query, limit=3):
    data = json.loads(CATALOG.read_text(encoding='utf-8'))
    wanted = terms(query)
    ranked = sorted(((len(wanted & terms(row['title'] + row['text'])), row) for row in data['documents']),
                    key=lambda pair: (-pair[0], pair[1]['id']))
    return {'version': data['version'], 'scope': 'local_simulator_documentation_not_dynamic_financial_data',
            'items': [dict(row, source_ref='kb:' + row['id'], score=score) for score, row in ranked[:limit] if score > 0],
            'coverage': '仅项目自身已实现能力；不含真实银行规则、实时行情或个人账务'}
