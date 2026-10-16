"""Separate OS-local simulator roles; these are not real bank identities."""
import hmac
import os
from pathlib import Path
from app.config import is_demo_admin_request
from app.domain.errors import DomainError


def require_approver(request):
    from app.config import ROOT
    path = Path(os.environ['FINAGENT_APPROVER_KEY_FILE']) if os.getenv('FINAGENT_APPROVER_KEY_FILE') else ROOT / 'var' / 'approver.key'
    supplied = request.headers.get('x-finagent-approver', '')
    if (not request.client or not is_demo_admin_request(request.client.host)
            or not path.is_file() or not supplied
            or not hmac.compare_digest(path.read_text(encoding='utf-8').strip(), supplied)):
        raise DomainError('APPROVER_REQUIRED', '须由持独立本机审批凭据的模拟审批员处理；用户确认和坐席凭据不能代替审批', 403)
