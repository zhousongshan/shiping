"""Public error facts never contain raw prompts, credentials, or signed URLs."""
from dataclasses import asdict, dataclass
import re
import time


MESSAGES = {
    'TRANSPORT_PREFLIGHT': '提交前连接检查失败，本次尚未发送视频生成请求；请检查网络后继续。',
    'SUBMISSION_UNCERTAIN': '生成请求的接收结果尚未确认，正在保留原记录等待核对，不会重复提交。',
    'AUTHENTICATION': '模型服务认证失败，需要管理员检查服务配置。',
    'RATE_LIMIT': '模型服务当前限流，请稍后恢复处理。',
    'NETWORK': '连接模型服务中断，已有工程和任务编号已保留。',
    'TIMEOUT': '模型服务响应超时，已有工程和任务编号已保留。',
    'UNAVAILABLE': '模型服务暂时不可用，已有工程和任务编号已保留。',
    'INVALID_INPUT': '模型服务拒绝了输入参数或素材，需要核对后再提交。',
    'INVALID_RESPONSE': '模型服务返回内容不完整，当前步骤未成功。',
    'GENERATION_FAILED': '已提交的视频生成失败，请根据服务商记录核对原因。',
    'REFERENCE_FETCH_FAILED': '生成服务无法下载参考素材，本次片段生成已失败；请修复素材地址后再决定是否重新生成。',
    'CONFIGURATION': '制作所需的服务配置不完整，需要管理员处理。',
    'EXECUTION_FAILED': '制作步骤未完成，工程和诊断记录已保留。',
}


@dataclass(frozen=True)
class Failure:
    code: str
    stage: str
    provider: str
    retryable: bool = False
    submission_uncertain: bool = False
    http_status: int | None = None

    @property
    def message(self):
        return MESSAGES[self.code]

    def public(self):
        return {**asdict(self), 'message': self.message, 'time': time.time()}


class WorkflowError(RuntimeError):
    def __init__(self, failure):
        self.failure = failure
        super().__init__(failure.message)


def classify(error, *, stage, provider='unknown', http_status=None, submission=False):
    if isinstance(error, WorkflowError):
        return error.failure
    text = str(error)
    upper = text.upper()
    status = http_status
    if status is None:
        match = re.search(r'HTTP\s+(\d{3})', text, re.I)
        status = int(match.group(1)) if match else None
    uncertain = 'SUBMISSION_UNCERTAIN' in upper or '回执' in text and '没有' in text
    if 'VIDEO_TRANSPORT_PREFLIGHT_FAILED:' in upper and not uncertain:
        return Failure('TRANSPORT_PREFLIGHT',stage,provider,False,False,status)
    transport = any(s in upper for s in ('FETCH FAILED', 'CONNECTION', 'ECONN', 'SSL', 'TRANSPORT', 'DNS'))
    timeout = 'TIMEOUT' in upper or 'TIMED OUT' in upper or '超时' in text
    # Unknown writes must never be made retryable by a generic network policy.
    rejected = status in (400, 401, 403, 404, 413, 415, 422)
    uncertain |= submission and not rejected and (transport or timeout or status in (500, 502, 503, 504))
    if uncertain: code = 'SUBMISSION_UNCERTAIN'
    elif status in (401, 403): code = 'AUTHENTICATION'
    elif status == 429: code = 'RATE_LIMIT'
    elif status is not None and status >= 500: code = 'UNAVAILABLE'
    elif status in (400, 404, 413, 415, 422): code = 'INVALID_INPUT'
    elif timeout: code = 'TIMEOUT'
    elif transport: code = 'NETWORK'
    elif 'MEDIA_GATEWAY_NOT_CONFIGURED' in upper or '未配置' in text: code = 'CONFIGURATION'
    elif 'JSON' in upper or 'INVALID_RESPONSE' in upper: code = 'INVALID_RESPONSE'
    elif stage == 'generation': code = 'GENERATION_FAILED'
    else: code = 'EXECUTION_FAILED'
    retryable = not submission and code in ('NETWORK', 'TIMEOUT', 'RATE_LIMIT', 'UNAVAILABLE', 'INVALID_RESPONSE')
    return Failure(code, stage, provider, retryable, uncertain, status)
