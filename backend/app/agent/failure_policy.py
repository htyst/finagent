"""Deterministic recovery advice. Retrying never confers authority."""


def classify(code, effect='read'):
    if code in {'RESULT_UNKNOWN', 'EXTERNAL_RESULT_UNKNOWN'}:
        return {'kind': 'unknown_result', 'retryable': False, 'action': 'reconcile',
                'message': '先核对原操作及回执，结果明确前不能重复办理。'}
    if code in {'DEPENDENCY_NOT_READY', 'ANALYSIS_REFERENCE', 'ANALYSIS_STALE_ASSUMPTION'}:
        return {'kind': 'dependency', 'retryable': False, 'action': 'replan',
                'message': '先补齐或重新获取前置证据，再继续相关步骤。'}
    if any(part in code for part in ('AUTHORIZATION', 'CONFIRMATION', 'EXPIRED', 'LOCKED', 'TASK_PAUSED')):
        return {'kind': 'authorization', 'retryable': False, 'action': 'wait_user',
                'message': '核对当前任务和授权状态，等待用户处理。'}
    if effect == 'read' and code in {'INTERRUPTED_READ', 'MODEL_NETWORK', 'MODEL_TIMEOUT', 'READ_TIMEOUT'}:
        return {'kind': 'transient_read', 'retryable': True, 'action': 'retry_read',
                'message': '查询可有界重试；失败不影响已完成业务回执。'}
    return {'kind': 'business_or_input', 'retryable': False, 'action': 'review',
            'message': '保留已成功事项，修正资料或人工处理未完成项；不会自动撤回交易。'}
