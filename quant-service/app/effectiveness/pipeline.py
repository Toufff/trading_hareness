"""Failure isolation is visible, not silent. Selection never depends on feedback."""
import logging
from .service import build


def attach(database,result,day,*,write=True):
    try:
        result['effectiveness']=build(database,day,write=write)
    except Exception as exc:
        logging.getLogger(__name__).exception('strategy_effectiveness_failed date=%s',day)
        result['effectiveness']={'status':'failed','as_of_date':str(day),'error_type':type(exc).__name__,
            'reason':'效果评估失败，不能解释为没有问题；检查 strategy_effectiveness_failed 日志。',
            'live_effect':'none','groups':[]}
