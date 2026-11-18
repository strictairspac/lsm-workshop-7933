import time,json,traceback
from pathlib import Path
from sqlalchemy import select
from .config.database import Session
from .config.settings import ARTIFACT_ROOT
from .models.trial import Trial
from .services.trial import queue
from .services.lsm_engine import simulate
from .services import block_store
heartbeat=Path(ARTIFACT_ROOT)/'worker.heartbeat'
def run_one(ident):
    with Session() as db:
        x=db.get(Trial,ident)
        if not x or x.result is not None:return
        x.status='running';db.commit()
        try:
            result=simulate(x.input_snapshot['entries'],x.input_snapshot['config'])
            # 仅通过 Oracle 校验的存活键值才允许压缩归档；索引与档案同时就绪。
            result['archive']=block_store.build_archive(ARTIFACT_ROOT,ident,result['actual'])
            # 建档后立即全量自检：索引定位区与每个压缩块校验通过才开放查询。
            block_store.verify_integrity(ARTIFACT_ROOT,ident)
            x.result=result;x.status='passed'
        except Exception as exc:
            x.result={'error':str(exc)[:500]};x.status='failed'
        db.commit();Path(f'{ARTIFACT_ROOT}/lsm-trial-{ident}.json').write_text(json.dumps(x.result,ensure_ascii=False,indent=2))
def main():
    while True:
        try:
            heartbeat.parent.mkdir(parents=True,exist_ok=True)
            heartbeat.write_text(str(time.time()));item=queue.blpop('lsm:jobs',timeout=2)
            if item:run_one(int(item[1]))
            else:
                with Session() as db:pending=list(db.scalars(select(Trial.id).where(Trial.status.in_(['queued','running']))))
                for ident in pending:run_one(ident)
        except Exception:traceback.print_exc();time.sleep(2)
if __name__=='__main__':main()
