from fastapi import APIRouter, Depends
from ..config.database import get_db
from ..schemas.archive import ArchiveQuery
from ..services.auth import current_user
from ..services.archive import archive_overview, query_archive
router = APIRouter(prefix='/trials')


@router.get('/{ident}/archive')
def overview(ident: int, db=Depends(get_db), user=Depends(current_user)):
    return archive_overview(db, ident)


@router.post('/{ident}/archive/query')
def query(ident: int, data: ArchiveQuery, db=Depends(get_db),
          user=Depends(current_user)):
    return query_archive(db, ident, data.start_key, data.end_key, data.read_quota)
