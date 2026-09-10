"""
test_ai_keys_remote.py — standalone bulk key validator and balance checker (NiceGUI).
"""
from nicegui import ui
import requests
import json
import os

CONFIG_PATH = os.path.expanduser('/home/joao/.config/kilo/config.json')

def load_kilo_keys():
    try:
        if not os.path.exists(CONFIG_PATH):
            return {}, "File does not exist."
        
        with open(CONFIG_PATH, 'r') as f:
            raw_content = f.read()
            
        clean_lines = []
        for line in raw_content.splitlines():
            stripped = line.strip()
            if stripped.startswith('//') or stripped.startswith('#'):
                continue
            clean_lines.append(line)
        clean_json_str = "\n".join(clean_lines)
        
        data = json.loads(clean_json_str)
        extracted = {}
        providers = data.get('provider', {})
        
        for p_key, p_val in providers.items():
            if not isinstance(p_val, dict):
                continue
            
            api_key = p_val.get('options', {}).get('apiKey', '')
            base_url = p_val.get('options', {}).get('baseURL', '')
            display_name = p_val.get('name', p_key.capitalize())
            
            if api_key:
                extracted[display_name] = {
                    'key': api_key,
                    'baseURL': base_url,
                    'type': p_key
                }
        return extracted, f"Successfully parsed {len(extracted)} providers from config."
    except Exception as e:
        return {}, f"Parse Error: {str(e)}"

def check_balance(name, key, base_url):
    """Attempt to query credit balances based on known provider API patterns."""
    headers = {"Authorization": f"Bearer {key}"}
    try:
        # OpenRouter balance endpoint
        if "openrouter.ai" in base_url:
            r = requests.get("https://openrouter.ai/api/v1/auth/key", headers=headers, timeout=4)
            if r.status_code == 200:
                data = r.json().get("data", {})
                limit = data.get("limit")
                usage = data.get("usage")
                if limit is not None:
                    remaining = float(limit) - float(usage or 0)
                    return f" | Balance/Limit: ${remaining:.2f} (Limit: ${float(limit):.2f})"
        
        # DeepSeek balance endpoint
        elif "deepseek.com" in base_url:
            r = requests.get("https://api.deepseek.com/user/balance", headers=headers, timeout=4)
            if r.status_code == 200:
                data = r.json()
                if data.get("is_available"):
                    balances = data.get("balance_infos", [])
                    total = sum(float(b.get("total_balance", 0)) for b in balances)
                    return f" | Balance: ${total:.2f}"
        
        # Z.AI / Zhipu or generic placeholders if available
        elif "z.ai" in base_url:
            return " | Balance: [API Check Managed via Dashboard]"
            
    except Exception:
        pass
    return ""

def test_single_endpoint(name, info):
    key = info['key']
    base_url = info['baseURL']
    url = f"{base_url.rstrip('/')}/models"
        
    headers = {"Authorization": f"Bearer {key}"}
    
    try:
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            data = response.json()
            models = []
            raw_list = data.get('data', data.get('models', []))
            for m in raw_list[:3]:
                if isinstance(m, dict):
                    models.append(m.get('id', str(m)))
                else:
                    models.append(str(m))
            model_str = f" | Models: {', '.join(models)}" if models else ""
            bal_str = check_balance(name, key, base_url)
            return f"🟢 {name}: SUCCESS (200 OK){model_str}{bal_str}"
        else:
            return f"🔴 {name}: FAILED (Status {response.status_code}) - {response.text[:80]}"
    except Exception as e:
        return f"🔴 {name}: Error - {str(e)}"

def test_all_keys():
    kilo_data, status_msg = load_kilo_keys()
    manual_key = key_input.value.strip()
    manual_prov = provider_select.value
    custom_url = custom_url_input.value.strip()
    
    results = [f"Status: {status_msg}"]
    
    if kilo_data:
        results.append("\n--- Testing Keys & Fetching Credits ---")
        for name, info in kilo_data.items():
            results.append(test_single_endpoint(name, info))
    else:
        results.append("\n⚠️ No API keys found inside the parsed provider blocks.")
        
    if manual_key:
        results.append("\n--- Testing Manual Input ---")
        base_urls = {
            "OpenCode Zen": "https://opencode.ai/zen/v1",
            "OpenRouter": "https://openrouter.ai/api/v1",
            "Omniroute": "http://192.168.0.6:20128/v1",
            "DeepSeek Direct": "https://api.deepseek.com/v1",
            "Z.AI": "https://api.z.ai/api/paas/v4"
        }
        b_url = custom_url if custom_url else base_urls.get(manual_prov, "https://openrouter.ai/api/v1")
        res = test_single_endpoint(manual_prov, {'key': manual_key, 'baseURL': b_url, 'type': manual_prov})
        results.append(res)
        
    result_box.content = "\n".join(results)

with ui.card().classes('absolute-center p-8 w-[700px] shadow-lg'):
    ui.label('Kilo Validator & Credit Checker').classes('text-2xl font-bold mb-2')
    ui.label(f'Target File: {CONFIG_PATH}').classes('text-xs text-gray-400 mb-6 font-mono')

    provider_select = ui.select(
        ['OpenCode Zen', 'OpenRouter', 'Omniroute', 'DeepSeek Direct', 'Z.AI'], 
        value='Z.AI'
    ).props('outlined label="Manual Override Provider"').classes('w-full mb-3')

    custom_url_input = ui.input('Custom Base URL (Optional)').props('outlined').classes('w-full mb-3')
    key_input = ui.input('Manual Override API Key').props('type=password outlined').classes('w-full mb-6')

    ui.button('Test Keys & Fetch Balances', on_click=test_all_keys).classes('w-full bg-blue-600 text-white py-3 mb-4')

    result_box = ui.markdown('Click above to test connectivity and query provider credit limits...').classes('mt-2 p-3 bg-gray-900 text-green-400 rounded w-full h-[260px] overflow-auto font-mono text-xs')

ui.run(port=8088, title="Kilo Credit & Key Validator", reload=False)