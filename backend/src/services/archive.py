from fastapi import HTTPException
from ..config.settings import ARTIFACT_ROOT
from ..models.trial import Trial
from . import block_store


def _require_passed_trial(db, ident: int) -> Trial:
    trial = db.get(Trial, ident)
    if not trial:
        raise HTTPException(404, '试验不存在')
    if trial.status != 'passed' or not trial.result or trial.result.get('error'):
        raise HTTPException(409, '仅通过 Oracle 校验的试验结果可归档查询')
    return trial


def archive_overview(db, ident: int) -> dict:
    """归档概况；旧试验无档案时给出回退说明，旧试验结果本身照常可读。"""
    trial = _require_passed_trial(db, ident)
    meta = block_store.archive_meta(ARTIFACT_ROOT, ident)
    if meta is None:
        return {
            'archived': False,
            'trial_id': ident,
            'fallback': '该试验为旧版结果，尚无分块压缩档案；完整结果仍可从试验证据与 JSON 导出读取。',
            'live_keys': trial.result.get('live_keys'),
        }
    return {'trial_id': ident, **meta}


def query_archive(db, ident: int, start_key: str | None, end_key: str,
                  read_quota: int | None) -> dict:
    _require_passed_trial(db, ident)
    if not block_store.is_archived(ARTIFACT_ROOT, ident):
        raise HTTPException(404, '索引与档案未同时就绪，查询未开放')
    if start_key is not None and end_key is not None and start_key > end_key:
        raise HTTPException(422, '起始键不能大于结束键')
    if read_quota is not None and read_quota < 0:
        raise HTTPException(422, '读取额度不能为负数')
    try:
        return block_store.range_query(
            ARTIFACT_ROOT, ident,
            start_key=start_key, end_key=end_key, read_quota=read_quota,
        )
    except block_store.QuotaExceeded as exc:
        # 容量不足：拒绝本次查询，不读取任何数据块。消息已含需求量与额度。
        raise HTTPException(402, str(exc)) from exc
    except block_store.ArchiveError as exc:
        # 坏块：整次查询失败，绝不交付半截结果。
        raise HTTPException(500, f'归档校验失败：{exc}') from exc
