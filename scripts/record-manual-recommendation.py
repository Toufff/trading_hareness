"""Register an actual recommendation now, without retroactive prospective claims."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'quant-service'))


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('scan','lane','symbols','author','reason'):p.add_argument('--'+name,required=True)
    p.add_argument('--env-file',default='G:/StockPlatform/config/runtime.env');a=p.parse_args()
    from dotenv import load_dotenv
    load_dotenv(a.env_file,override=True)
    from app.database import Database
    from app.effectiveness.manual import register
    db=Database()
    try:result=register(db,json.loads(Path(a.scan).read_text(encoding='utf-8-sig')),a.lane,a.symbols.split(','),a.author,a.reason)
    finally:db.close()
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
