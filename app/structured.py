"""Bounded schema repair for model output; never retries video generation."""
import json
from . import planner
from .errors import WorkflowError
from .production_schema import artifact


class InspectionIncomplete(ValueError):
    pass


def call(root, kind, instruction, content, validate, *, inspection=False):
    messages=list(content)
    for attempt in range(2):
        try:
            result=planner.chat(instruction,messages)
        except WorkflowError as exc:
            if inspection:
                artifact(root,kind+'-errors',{'attempt':attempt+1,'failure':exc.failure.public()})
                raise InspectionIncomplete('视频已保留，检查服务未完成，请恢复检查。') from exc
            raise
        try:
            if not isinstance(result,dict):raise ValueError('返回值必须是JSON对象')
            return validate(result)
        except (ValueError,KeyError,TypeError,AttributeError) as exc:
            artifact(root,kind+'-errors',{'attempt':attempt+1,'response':result,'error':str(exc)[:600]})
            if attempt:
                error=InspectionIncomplete if inspection else ValueError
                raise error('检查结果两次未通过结构校验，视频已保留，可恢复检查。' if inspection
                            else '创作要求两次未通过校验：'+str(exc)[:300]) from exc
            messages.append({'type':'text','text':'只纠正返回结构，不改变观察事实，不为了通过校验而编造证据。错误：'
                +str(exc)[:600]+'；上次返回：'+json.dumps(result,ensure_ascii=False)})
