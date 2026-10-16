"""Trusted, stdlib-only DSL gate run with python -I -S. No generated code."""
import json
import sys

TOOLS = {'list_accounts', 'list_cards', 'list_recipients', 'list_transactions', 'summarize_spending',
         'generate_statement', 'inspect_anomalies', 'get_task_status', 'prepare_transfer', 'prepare_card_action',
         'read_wealth', 'prepare_wealth', 'read_subscriptions', 'identify_subscriptions', 'prepare_subscription',
         'read_birthdays', 'prepare_birthday', 'read_card_management', 'prepare_card_management',
         'prepare_schedule', 'prepare_collection', 'read_agent_status', 'list_schedules', 'list_collections', 'search_knowledge'}


def deny(event, args):
    if event in {'open', 'os.system', 'subprocess.Popen', 'compile', 'exec'} or event.startswith(('socket.', 'os.remove', 'os.rename', 'os.mkdir')):
        raise PermissionError('SANDBOX_DENIED')


def main():
    sys.addaudithook(deny)
    data = sys.stdin.buffer.read(65537)
    if len(data) > 65536:
        raise ValueError('SANDBOX_INPUT_LIMIT')
    request = json.loads(data)
    if set(request) != {'tool', 'arguments'} or request['tool'] not in TOOLS or not isinstance(request['arguments'], dict):
        raise ValueError('SANDBOX_INVALID_TOOL')
    print('{"accepted":true}')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, TypeError, PermissionError, RecursionError):
        print('{"accepted":false}')
        sys.exit(1)
