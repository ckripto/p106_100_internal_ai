"""Read-only inference smoke checks; does not execute generated tools."""
import json
import sys
import urllib.request

base = sys.argv[1] if len(sys.argv) > 1 else 'http://localhost:8080'


def request(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=180) as response:
        return json.load(response)


def chat(messages, **kwargs):
    return request('/v1/chat/completions', {
        'messages': messages, 'temperature': 0, 'max_tokens': 160,
        'chat_template_kwargs': {'enable_thinking': False}, **kwargs})


report = {'health': request('/health'), 'checks': []}
answer = chat([{'role': 'user', 'content': 'What is 6 * 7? Reply with the number only.'}])
choice = answer['choices'][0]
assert choice['finish_reason'] == 'stop', choice
assert choice['message']['content'].strip() == '42', choice
report['checks'].append({'name': 'arithmetic', 'choice': choice,
                         'timings': answer.get('timings'), 'usage': answer.get('usage')})
answer = chat([{'role': 'user', 'content': 'Call write_file to create test.py containing print(42) followed by a newline. Use offset 0 and final true.'}],
              tools=[{'type': 'function', 'function': {'name': 'write_file', 'description': 'Write a short file chunk.',
                  'parameters': {'type': 'object', 'properties': {
                      'path': {'type': 'string'}, 'content': {'type': 'string'},
                      'offset': {'type': 'integer'}, 'final': {'type': 'boolean'}},
                      'required': ['path', 'content', 'offset', 'final'], 'additionalProperties': False}}}],
              tool_choice='required', parallel_tool_calls=False)
choice = answer['choices'][0]
assert choice['finish_reason'] in {'tool_calls', 'stop'}, choice
calls = choice['message']['tool_calls']
assert len(calls) == 1 and calls[0]['function']['name'] == 'write_file', choice
args = json.loads(calls[0]['function']['arguments'])
assert args == {'path': 'test.py', 'content': 'print(42)\n', 'offset': 0, 'final': True}, args
report['checks'].append({'name': 'structured_tool_call', 'choice': choice,
                         'timings': answer.get('timings'), 'usage': answer.get('usage')})
print(json.dumps(report, ensure_ascii=False, indent=2))
