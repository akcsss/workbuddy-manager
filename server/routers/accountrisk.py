from fastapi import APIRouter, Depends, HTTPException
from .. import security
from ..services import accountrisk

router = APIRouter(prefix='/api/account-risk', tags=['accounts'])


@router.get('')
async def warnings(upstream_id: int | None = None, all_groups: bool = False,
                   realm: str | None = None, user: dict = Depends(security.current_user)) -> dict:
    return await accountrisk.warnings(group_id=upstream_id, all_groups=all_groups, realm=realm)


@router.post('/{ident}/acknowledge')
def acknowledge(ident: str, user: dict = Depends(security.require_admin)) -> dict:
    if not accountrisk.acknowledge(ident):
        raise HTTPException(404, '预警不存在')
    return {'ok': True}
