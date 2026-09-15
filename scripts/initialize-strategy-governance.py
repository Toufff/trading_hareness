"""Apply additive governance schema and seed user-authorized atomic findings."""
from pathlib import Path
import argparse,json,os,subprocess,sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'quant-service'))


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--migrate',action='store_true')
    args=p.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    for line in Path('G:/StockPlatform/config/runtime.env').read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.startswith('#'):
            k,v=line.split('=',1);os.environ[k]=v
    if args.migrate:
        subprocess.run([sys.executable,'-m','alembic','upgrade','head'],cwd=ROOT/'quant-service',check=True,
                       creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    from app.database import Database
    from app.strategy_governance.repository import create_issue
    changes=[
      ('PV01','成交股数与成交额的口径分离','all','有金额不等于有同口径股数，缺单位时不应生成股数量比。'),
      ('PV02','启动的收盘质量与长上影核对','expansion,event,trend','基础筛选对同收盘不同上影给出相同结果，缺少价格成果判断。'),
      ('PV03','强势回踩的分段成交比较','pullback','前五日均额1.04倍仍被称作缩量，需明确比较基准和允许误差。'),
      ('PV04','横盘资金与量价质量分开','accumulation','资金合计为正与形态横盘分别评分，不等于吸筹完成或低位。'),
      ('PV05','收缩突破结构线统一','contraction','十日OHLC高点判断与五日收盘参考线文字不一致。'),
      ('PV06','恐慌反弹的修复幅度','reclaim','先跌约6.15%后涨0.11%的构造案例可进入修复名单。'),
      ('PV07','板块方向与个股及封板证据分离','rotation,relay','板块改善不等于个股承接或回封已经被验证。'),
      ('PV08','同一证据对象贯通报告图表跟踪','presentation','未来条件不能冒充已确认事实，缺口不能渲染成已通过。')]
    db=Database();items=[]
    for key,title,scope,problem in changes:
        item=create_issue(db,dict(title=title,problem=problem,hypothesis='将此单一缺口转成可计算证据与反例验收，保留原始候选，不宣称收益有效。',
             scope=scope,out_of_scope='不改其他策略权重、不交易；当前授权只覆盖本轮明确量价修复，不授权未来自动上线。',
             dedupe_key='20260911:user-price-volume:'+key,dependencies=[],evidence=[dict(change_id=key,
             source='当前对话已复现的生产规则审查',plan='docs/STRATEGY_GOVERNANCE_IMPLEMENTATION.md',
             input_evidence='G:/StockPlatform/data/research/2026-09-11-governance-pv-acceptance',
             authorization='用户2026-09-11授权两个工作包实施部署；不是未来实验自动批准')]),
             {'id':'root:20260911-discovery','roles':['observer']})
        items.append(dict(id=item['id'],change_id=key,state=item['state']))
    target=Path('G:/StockPlatform/data/research/2026-09-11-governance-pv-acceptance')
    target.mkdir(parents=True,exist_ok=True)
    (target/'seed-receipt.json').write_text(json.dumps(items,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'seeded':len(items),'items':items},ensure_ascii=False))


if __name__=='__main__':main()
